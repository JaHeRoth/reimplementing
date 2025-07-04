from datetime import timedelta

from utilities.data_handler import load_preprocessed
from transformers import AutoTokenizer
import os
import torch
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel
import time
from pathlib import Path
from utilities.model_handler import train
from utilities.models import TransformerEncoderGPT2

context_length = 512

def prep():
    tokenizer = AutoTokenizer.from_pretrained(
        "openai-community/gpt2",
        pad_token="<|pad|>",
        unk_token="<unk>",  # Because it appears often in the dataset
    )

    _, tokenized_ds = load_preprocessed(
        hf_path="wikitext", hf_name="wikitext-103-v1", tokenizer=tokenizer, context_length=context_length
    )

    return tokenizer, tokenized_ds


def setup(rank, world_size):
    os.environ["MASTER_ADDR"] = "127.0.0.1"  # where rank 0 lives
    os.environ["MASTER_PORT"] = "29500"  # any free port
    backend = "nccl" if torch.cuda.is_available() else "gloo"
    dist.init_process_group(backend=backend, rank=rank, world_size=world_size, timeout=timedelta(seconds=30))


def worker(rank, world_size, tokenizer, tokenized_ds):
    setup(rank, world_size)
    try:
        device = torch.device(f"cuda:{rank}" if torch.cuda.is_available() else "cpu")

        # Using hyperparams of GPT paper (although we use a different dataset)
        model = TransformerEncoderGPT2(
            d_model=768,
            nhead=12,
            num_layers=12,
            dim_feedforward=3072,
            vocab_size=len(tokenizer),
            context_length=context_length,
            eos_token_id=tokenizer.eos_token_id,
            device=device,
        )
        # We choose to always use DDP, to avoid downstream if-statements for the rare case of single-device training.
        model = DistributedDataParallel(
            model,
            device_ids=[rank] if torch.cuda.is_available() else None,
        )

        decaying_params = []
        non_decaying_params = []
        for name, param in model.named_parameters():
            if name.endswith("bias") or ".norm" in name:
                non_decaying_params.append(param)
            else:
                decaying_params.append(param)

        optimizer = torch.optim.AdamW(
            params=[
                {"params": decaying_params, "weight_decay": 0.01},
                {"params": non_decaying_params, "weight_decay": 0.0},
            ],
            betas=(0.9, 0.98),
            eps=1e-9,
            lr=2.5e-4,
        )
        train(
            model=model,
            optimizer=optimizer,
            tokenizer=tokenizer,
            tokenized_train_ds=tokenized_ds["train"],
            tokenized_eval_ds=tokenized_ds["validation"],
            device=device,
            train_batch_size=64,
            run_id=str(int(time.time())),
            # We disable these for all but rank 0, to avoid cluttering the output
            make_outputs=rank == 0,
            stream_prompt=f"{tokenizer.eos_token}She first",
            log_period=50,
            stream_period=250,
            eval_period=500,
            checkpoint_period=100,
        )
    finally:
        print(f"Worker {rank}/{world_size} waiting to clean up after itself.", flush=True)
        dist.barrier()
        print(f"Worker {rank}/{world_size} cleaning up after itself.", flush=True)
        dist.destroy_process_group()
        torch.cuda.empty_cache()


def run():
    tokenizer, tokenized_ds = prep()

    if torch.cuda.is_available():
        world_size = torch.cuda.device_count()
        print(f"Running on {world_size} GPUs")
        mp.spawn(worker, nprocs=world_size, args=(world_size, tokenizer, tokenized_ds))
    else:
        print(f"Running on a single CPU")
        # Not running through mp.spawn makes debugging easier
        worker(rank=0, world_size=1, tokenizer=tokenizer, tokenized_ds=tokenized_ds)


if __name__ == "__main__":
    run()

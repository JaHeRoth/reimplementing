import time
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

import torch
from torch import nn
import torch.distributed as dist
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LinearLR, CosineAnnealingLR, SequentialLR
import numpy as np
import matplotlib.pyplot as plt


def nucleus_sample(probits: torch.Tensor, prob_threshold: float):
    sorted_probs, sorted_indices = torch.sort(probits, dim=-1, descending=True)
    prev_cumulative_probs = torch.cumsum(sorted_probs, dim=-1) - sorted_probs
    sorted_indices_to_remove = prev_cumulative_probs >= prob_threshold
    sorted_probs[sorted_indices_to_remove] = 0
    sampled_sorted_idx = torch.multinomial(sorted_probs, num_samples=1)
    sampled_idx = sorted_indices.gather(dim=-1, index=sampled_sorted_idx)
    return sampled_idx


def greedy_sample(probits: torch.Tensor):
    return probits.argmax(dim=-1, keepdim=True)


def stream(model, input_ids: torch.Tensor, max_length: int, prob_threshold: float, temperature: float):
    next_input_ids = input_ids
    for i in range(max_length - input_ids.shape[-1]):
        with torch.no_grad():
            next_token_logits = model(next_input_ids, streaming=i != 0, seq_len=i + input_ids.shape[-1])[:, -1, :]
            # TODO: If repetitions resurface as an issue: repetition_penalty and no_repeat_ngram_size
            next_token_shaped_logits = next_token_logits / temperature
            next_token_probits = F.softmax(next_token_shaped_logits, dim=-1)
            next_input_ids = nucleus_sample(probits=next_token_probits, prob_threshold=prob_threshold)
            yield next_input_ids.item()


def print_stream(
    model, tokenizer, prompt: str, device, max_length: int, prob_threshold: float = 0.95, temperature: float = 1.0
):
    print(prompt, end="", flush=True)
    input_ids = tokenizer(prompt, return_tensors="pt").input_ids.to(device)
    for token in stream(
        model=model, input_ids=input_ids, max_length=max_length, prob_threshold=prob_threshold, temperature=temperature
    ):
        print(tokenizer.decode(token), end="", flush=True)
        # After print, since we want to see if our model learns to generate EOS tokens
        if token == tokenizer.eos_token_id:
            break
    print("", flush=True)


def batch_to_tensor(batch, device):
    batch_input_ids = [row["input_ids"] for row in batch]
    return torch.tensor(batch_input_ids, dtype=torch.int64, device=device)


def avg_between_processes(tensor: torch.Tensor) -> None:
    # We prefer ReduceOp.SUM over ReduceOP.AVG, since the latter isn't supported by Gloo
    dist.all_reduce(tensor=tensor, op=dist.ReduceOp.SUM)
    tensor /= dist.get_world_size()

def train(
    model,
    optimizer,
    tokenizer,
    tokenized_train_ds,
    tokenized_eval_ds,
    device,
    make_outputs: bool,
    stream_prompt: str,
    train_batch_size: int = 64,
    gradient_accumulation_steps: int = 1,
    num_epochs: int = 100,
    warmup_steps: int = 2000,
    log_period: int = 25,
    stream_period: int = 100,
    eval_period: int = 250,
    checkpoint_period: int = 50,
    run_id: str = None,
) -> tuple[list[float], list[float]]:
    """Trains `model` (in-place) and returns training and eval losses."""
    plot_dir = Path(f"outputs/plots/{run_id}")
    plot_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = Path(f"outputs/model_checkpoints/{run_id}")
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    train_sampler = torch.utils.data.distributed.DistributedSampler(
        tokenized_train_ds,
        shuffle=True,
    )
    eval_sampler = torch.utils.data.distributed.DistributedSampler(
        tokenized_eval_ds,
        shuffle=False,
    )

    train_dl = DataLoader(
        tokenized_train_ds,
        batch_size=train_batch_size,
        sampler=train_sampler,
        collate_fn=lambda batch: batch_to_tensor(batch, device),
    )
    validation_dl = DataLoader(
        tokenized_eval_ds,
        batch_size=train_batch_size * 2,
        sampler=eval_sampler,
        collate_fn=lambda batch: batch_to_tensor(batch, device),
    )
    num_updates = len(train_dl) // gradient_accumulation_steps

    total_steps = num_epochs * len(train_dl)
    decay_steps = total_steps - warmup_steps
    scheduler = SequentialLR(
        optimizer=optimizer,
        schedulers=[
            LinearLR(optimizer, start_factor=1.0 / warmup_steps, end_factor=1.0, total_iters=warmup_steps),
            CosineAnnealingLR(optimizer, T_max=decay_steps, eta_min=0.0),
        ],
        milestones=[warmup_steps],
    )

    model.train()
    train_losses = {}
    eval_losses = {}
    unclipped_update_norms = []
    global_update_i = 0
    for epoch_i in range(1, num_epochs + 1):
        train_sampler.set_epoch(epoch_i)
        eval_sampler.set_epoch(epoch_i)
        log_period_start_time = time.time()
        avg_train_loss = torch.tensor(0, dtype=torch.bfloat16, device=device)
        for batch_i, batch in enumerate(train_dl, start=1):
            update_i = batch_i // gradient_accumulation_steps
            should_update = batch_i % gradient_accumulation_steps == 0

            X: torch.Tensor = batch[:, :-1].contiguous()
            y: torch.Tensor = batch[:, 1:].contiguous()
            with nullcontext() if should_update else model.no_sync():
                logits = model(X)
                loss = nn.functional.cross_entropy(
                    logits.view(-1, logits.shape[-1]),
                    y.view(-1),
                    ignore_index=tokenizer.pad_token_id,
                ) / gradient_accumulation_steps
                avg_train_loss += loss / log_period
                loss.backward()

            if should_update:
                global_update_i += 1
                unclipped_update_norms.append(
                    nn.utils.clip_grad_norm_(model.parameters(), 1.0).item()
                )
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()

            if batch_i % (log_period * gradient_accumulation_steps) == 0:
                avg_between_processes(tensor=avg_train_loss)
                if make_outputs:
                    train_losses[global_update_i] = avg_train_loss.item()
                    log_period_seconds = time.time() - log_period_start_time
                    ms_per_update = 1000 * log_period_seconds / log_period
                    tokens_per_update = X.numel() * gradient_accumulation_steps * dist.get_world_size()
                    print(f"Update {update_i}/{num_updates} in epoch {epoch_i}/{num_epochs}: "
                          f"Loss={avg_train_loss.item():.3f}, Grad norm={unclipped_update_norms[-1]:.2f}, "
                          f"ms/update={round(ms_per_update)}, "
                          f"tokens/ms={round(tokens_per_update / ms_per_update)}")
                    log_period_start_time = time.time()
                avg_train_loss = torch.tensor(0, dtype=torch.bfloat16, device=device)

            if make_outputs and batch_i % (stream_period * gradient_accumulation_steps) == 0:
                model.eval()
                print_stream(
                    model=model,
                    tokenizer=tokenizer,
                    prompt=stream_prompt,
                    device=device,
                    max_length=X.shape[1],
                )
                print("", flush=True)
                model.train()

            if batch_i % (eval_period * gradient_accumulation_steps) == 0:
                with torch.no_grad():
                    model.eval()
                    avg_val_loss = torch.tensor(0, dtype=torch.bfloat16, device=device)
                    for validation_batch in validation_dl:
                        X_val: torch.Tensor = validation_batch[:, :-1].contiguous()
                        y_val: torch.Tensor = validation_batch[:, 1:].contiguous()
                        val_logits = model(X_val)
                        avg_val_loss += (
                            nn.functional.cross_entropy(
                                val_logits.view(-1, val_logits.shape[-1]),
                                y_val.view(-1),
                                ignore_index=tokenizer.pad_token_id,
                            )
                            / len(validation_dl)
                        )
                    avg_between_processes(tensor=avg_val_loss)
                    if make_outputs:
                        eval_losses[global_update_i] = avg_val_loss.item()
                        print(f"Average validation loss: {avg_val_loss.item()}")
                    model.train()

            if make_outputs and batch_i % (checkpoint_period * gradient_accumulation_steps) == 0:
                path = checkpoint_dir / f"epoch_{epoch_i}_update_{update_i}.pt"
                print(f"Saving checkpoint to '{path}'.")
                torch.save(
                    obj={
                        "model.config": asdict(model.module.config),
                        "model.state_dict": model.module.state_dict(),
                        "optimizer.state_dict": optimizer.state_dict(),
                        "epoch": epoch_i,
                        "update": update_i,
                        "global_update": global_update_i,
                    },
                    f=path,
                )

        if make_outputs:
            print("=" * 40 + f"COMPLETED EPOCH {epoch_i}/{num_epochs}" + "=" * 40)

            plt.plot(train_losses.keys(), train_losses.values(), "--o", label="Train Loss")
            plt.plot(eval_losses.keys(), eval_losses.values(), "--o", label="Eval Loss")
            plt.xlabel("Update")
            plt.ylabel("Loss")
            plt.title(f"Loss over first {epoch_i} epoch(s)")
            plt.xscale("log")
            plt.yscale("log")
            plt.legend()
            plt.grid()
            plt.savefig(plot_dir / f"losses__epoch_{epoch_i}.png", bbox_inches="tight")
            plt.show()
            plt.clf()

            plt.plot(unclipped_update_norms)
            plt.xlabel("Update")
            plt.ylabel("Unclipped update norm")
            plt.grid()
            plt.savefig(plot_dir / f"unclipped_update_norm__epoch_{epoch_i}.png", bbox_inches="tight")
            plt.show()
            plt.clf()

    return train_losses, eval_losses

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# BFF_Optimizer: a pure Bfloat16 AdamW optimizer with optional Kahan summation
# Features direct control over momentum, variance and auxiliary compensation
# buffer dtypes.
# Kahan summation is used to offset Bfloat16 precision reduction for
# the weight updates, allowing full training in BFloat16.

import torch
from torch.optim.optimizer import Optimizer


class BFF_AdamW(Optimizer):
    def __init__(
        self,
        params,
        lr=1e-3,
        betas=(0.9, 0.999),
        eps=1e-8,
        weight_decay=0.0,
        use_kahan_summation=True,
        momentum_dtype=torch.bfloat16,
        variance_dtype=torch.bfloat16,
        compensation_buffer_dtype=torch.bfloat16,
    ):
        """
        Args:
                params (iterable): iterable of parameters to optimize or dicts defining
                    parameter groups
                lr (float, optional): learning rate (default: 1e-3)
                betas (Tuple[float, float], optional): coefficients used for computing
                    running averages of gradient and its square (default: (0.9, 0.999))
                eps (float, optional): term added to the denominator to improve
                    numerical stability (default: 1e-8)
                weight_decay (float, optional): weight decay coefficient (default: 1e-2)

                # BFF specific
                use_kahan_summation: creates auxiliary buffer to ensure high precision (default: True)
                momentum_dtype = dtype for momentum (default: torch.bfloat16)
                variance_dtype = dtype for uncentered variance (default: torch.bfloat16)
                compensation_buffer_dtype = dtype for Kahan summation buffer (default: torch.bfloat16)
                
                # usage example:
                Basic usage - Run with BFF defaults, (adjust lr, betas, eps, weight decay same as AdamW),
                set model.to(torch.BFloat16) and run with FSDP mixed precision with BFloat16 policies.
                
                This will create a high precision BF16 optimizer and pure BF16 pipeline, 
                that should meet/exceed running in pure FP32. 
                
                Additional options - to see the degradation with usual BF16, use_kahan_summation = False.
                This will have model updates in only BF16, which will result in capping of where the model
                can converge to relative to high precision BF16 updates and/or FP32.
                
                To revert to usual AdamW = use_kahan_summation=False, momentum/variance_dtype = torch.float32


        """
        defaults = dict(
            lr=lr,
            betas=betas,
            eps=eps,
            weight_decay=weight_decay,
            use_kahan_summation=use_kahan_summation,
            momentum_dtype=momentum_dtype,
            variance_dtype=variance_dtype,
            compensation_buffer_dtype=compensation_buffer_dtype,
        )

        super().__init__(params, defaults)

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.
        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        
        if closure is not None:
            with torch.enable_grad():
                # to fix linter, we do not keep the returned loss for use atm.
                closure()

        for group in self.param_groups:

            beta1, beta2 = group["betas"]
            lr = group["lr"]
            weight_decay = group["weight_decay"]
            eps = group["eps"]
            # BFF specifics
            use_kahan_summation = group["use_kahan_summation"]

            momentum_dtype = group["momentum_dtype"]
            variance_dtype = group["variance_dtype"]
            compensation_buffer_dtype = group["compensation_buffer_dtype"]

            for p in group["params"]:
                if p.grad is None:
                    continue

                if p.grad.is_sparse:
                    raise RuntimeError("BFF does not support sparse gradients")

                state = self.state[p]

                # State initialization
                if len(state) == 0:

                    state["step"] = torch.tensor(0.0)

                    # momentum - EMA of gradient values
                    state["exp_avg"] = torch.zeros_like(
                        p,
                        memory_format=torch.preserve_format,
                        dtype=momentum_dtype,
                    )

                    # variance uncentered - EMA of squared gradient values
                    state["exp_avg_sq"] = torch.zeros_like(
                        p,
                        memory_format=torch.preserve_format,
                        dtype=variance_dtype,
                    )

                    # optional Kahan summation - accumulated error tracker
                    if use_kahan_summation:
                        state["compensation"] = torch.zeros_like(
                            p,
                            memory_format=torch.preserve_format,
                            dtype=compensation_buffer_dtype,
                        )

                # main processing -------------------------

                # update the steps for each param group update
                state["step"] += 1
                step = state["step"]

                exp_avg = state["exp_avg"]
                exp_avg_sq = state["exp_avg_sq"]

                grad = p.grad

                # weight decay, AdamW style
                if weight_decay:
                    p.data.mul_(1 - lr * weight_decay)

                # update momentum
                exp_avg.mul_(beta1).add_(grad, alpha=1 - beta1)

                # update uncentered variance
                exp_avg_sq.mul_(beta2).addcmul_(grad, grad, value=1 - beta2)

                # adjust using bias1
                bias_correction1 = 1 - beta1**step
                step_size = lr / bias_correction1

                # adjust using bias2
                denom_correction = (1 - beta2**step) ** 0.5  # avoids math import

                centered_variance = (exp_avg_sq.sqrt() / denom_correction).add_(
                    eps, alpha=1
                )
                # step_adjustment = -step_size * denom_correction

                # lr update to compensation
                if use_kahan_summation:
                    compensation = state["compensation"]

                    compensation.addcdiv_(exp_avg, centered_variance, value=-step_size)

                    # update weights with compensation (Kahan summation)
                    # save error back to compensation for next iteration
                    temp_buffer = p.clone()
                    p.data.add_(compensation)
                    compensation.add_(temp_buffer.sub_(p.data))

                else:
                    # usual AdamW updates
                    p.data.addcdiv_(exp_avg, centered_variance, value=-step_size)

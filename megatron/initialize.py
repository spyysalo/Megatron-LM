# coding=utf-8
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Megatron initialization."""

import logging
import logging.config
import random
import os
import sys
import time

import numpy as np
import torch
from datetime import timedelta

try:
    import wandb
except ModuleNotFoundError:
    print('Wandb import failed', flush=True)

from megatron import fused_kernels
from megatron import get_adlr_autoresume
from megatron import get_args
from megatron import get_tensorboard_writer
from megatron import mpu
from megatron.arguments import (parse_args, validate_args)
from megatron.checkpointing import load_args_from_checkpoint
from megatron.global_vars import set_global_variables
from megatron.mpu import (set_tensor_model_parallel_rank,
                          set_tensor_model_parallel_world_size)
from megatron.model.transformer import bias_dropout_add_fused_train
from megatron.model.fused_bias_gelu import bias_gelu


def initialize_megatron(extra_args_provider=None, args_defaults={},
                        ignore_unknown_args=False, allow_no_cuda=False):
    """Set global variables, initialize distributed, and
    set autoresume and random seeds.
    `allow_no_cuda` should not be set unless using megatron for cpu only 
    data processing. In general this arg should not be set unless you know 
    what you are doing.
    Returns a function to finalize distributed env initialization 
    (optionally, only when args.lazy_mpu_init == True)
    """
    if not allow_no_cuda:
        # Make sure cuda is available.
        assert torch.cuda.is_available(), 'Megatron requires CUDA.'

    # Parse arguments
    args = parse_args(extra_args_provider, ignore_unknown_args)

    if args.use_checkpoint_args or args_defaults.get('use_checkpoint_args', False):
        assert args.load is not None, '--use-checkpoints-args requires --load argument'
        load_args_from_checkpoint(args)

    validate_args(args, args_defaults)
        
    # set global args, build tokenizer, and set adlr-autoresume,
    # tensorboard-writer, and timers.
    set_global_variables(args)

    # torch.distributed initialization
    def finish_mpu_init():
        args = get_args()
        # Pytorch distributed.
        _initialize_distributed()
        _configure_logging()
        
        # Random seeds for reproducibility.
        if args.rank == 0:
            print('> setting random seeds to {} ...'.format(args.seed))
        _set_random_seed(args.seed, args.data_parallel_random_init)

    args = get_args()
    if  args.lazy_mpu_init:
        args.use_cpu_initialization=True
        # delayed initialization of DDP-related stuff
        # We only set basic DDP globals    
        set_tensor_model_parallel_world_size(args.tensor_model_parallel_size)
        # and return function for external DDP manager
        # to call when it has DDP initialized
        set_tensor_model_parallel_rank(args.rank)    
        return finish_mpu_init
    else:
        # Megatron's MPU is the master. Complete initialization right away.
        finish_mpu_init()

        # Autoresume.
        _init_autoresume()

        # Compile dependencies.
        _compile_dependencies()

        # No continuation function
        return None


def _configure_logging():
    args=get_args()
    if not args.structured_logs:
        return
    rank = torch.distributed.get_rank()

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": f"%(asctime)s [Rank {rank}]: %(message)s",
                "use_colors": True,
            }
        },
        "handlers": {
            "default": {
                "level": "INFO",
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            }
        },
        "loggers": {"default": {"level": "DEBUG", "handlers": ["default"]}},
        "root": {"handlers": ["default"], "level": "INFO"},
    }
    if args.structured_logs_dir is not None:
        log_dir=args.structured_logs_dir
        os.makedirs(log_dir, exist_ok=True)
        logging_config["handlers"]["file"] = {
            "level": "INFO",
            "formatter": "default",
            "class": "logging.FileHandler",
            "filename": os.path.join(log_dir, f"logs_rank_{rank}.txt"),
        }
        logging_config["root"]["handlers"].append("file")
        logging_config["loggers"]["default"]["handlers"].append("file")
    logging.config.dictConfig(logging_config)

    # Add these methods so that stdout can be redirected to logging.
    logging.write = lambda msg: logging.info(msg) if msg != '\n' else None
    logging.flush = lambda : None

    sys.stdout=logging
    sys.stderr=logging



def _compile_dependencies():

    args = get_args()

    # =========================
    # Compile dataset C++ code.
    # =========================
    # TODO: move this to ninja
    if torch.distributed.get_rank() == 0:
        start_time = time.time()
        print('> compiling dataset index builder ...')
        from megatron.data.dataset_utils import compile_helper
        compile_helper()
        print('>>> done with dataset index builder. Compilation time: {:.3f} '
              'seconds'.format(time.time() - start_time), flush=True)

    # ==================
    # Load fused kernels
    # ==================

    # Custom kernel constraints check.
    seq_len = args.seq_length
    attn_batch_size = \
        (args.num_attention_heads / args.tensor_model_parallel_size) * \
        args.micro_batch_size
    # Constraints on sequence length and attn_batch_size to enable warp based
    # optimization and upper triangular optimization (for causal mask)
    custom_kernel_constraint = seq_len > 16 and seq_len <=8192 and \
        seq_len % 4 == 0 and attn_batch_size % 4 == 0
    # Print a warning.
    if not ((args.fp16 or args.bf16) and
            custom_kernel_constraint and
            args.masked_softmax_fusion):
        if args.rank == 0:
            print('WARNING: constraints for invoking optimized'
                  ' fused softmax kernel are not met. We default'
                  ' back to unfused kernel invocations.', flush=True)
    
    # Always build on rank zero first.
    if torch.distributed.get_rank() == 0:
        start_time = time.time()
        print('> compiling and loading fused kernels ...', flush=True)
        fused_kernels.load(args)
        torch.distributed.barrier()
    else:
        torch.distributed.barrier()
        fused_kernels.load(args)
    # Simple barrier to make sure all ranks have passed the
    # compilation phase successfully before moving on to the
    # rest of the program. We think this might ensure that
    # the lock is released.
    torch.distributed.barrier()
    if torch.distributed.get_rank() == 0:
        print('>>> done with compiling and loading fused kernels. '
              'Compilation time: {:.3f} seconds'.format(
                  time.time() - start_time), flush=True)



def _initialize_distributed():
    """Initialize torch.distributed and mpu."""
    args = get_args()

    device_count = torch.cuda.device_count()
    if torch.distributed.is_initialized():

        if args.rank == 0:
            print('torch distributed is already initialized, '
                  'skipping initialization ...', flush=True)
        args.rank = torch.distributed.get_rank()
        args.world_size = torch.distributed.get_world_size()

    else:

        if args.rank == 0:
            print('> initializing torch distributed ...', flush=True)
        # Manually set the device ids.
        if device_count > 0:
            device = args.rank % device_count
            if args.local_rank is not None:
                assert args.local_rank == device, \
                    'expected local-rank to be the same as rank % device-count.'
            else:
                args.local_rank = device
            torch.cuda.set_device(device)
        # Include this torch.distributed.init_process_group() code in the `else` branch because
        # we do not want to reinitialize if torch.distributed.is_initialized() returns True
        # Call the init process
        torch.distributed.init_process_group(
            backend=args.distributed_backend,
            world_size=args.world_size, rank=args.rank,
            timeout=timedelta(seconds=args.distributed_timeout))

    # Set the tensor model-parallel, pipeline model-parallel, and
    # data-parallel communicators.
    if device_count > 0:
        if mpu.model_parallel_is_initialized():
            print('model parallel is already initialized')
        else:
            mpu.initialize_model_parallel(args.tensor_model_parallel_size,
                                          args.pipeline_model_parallel_size,
                                          args.virtual_pipeline_model_parallel_size,
                                          args.pipeline_model_parallel_split_rank)


def _init_autoresume():
    """Set autoresume start time."""
    autoresume = get_adlr_autoresume()
    if autoresume:
        torch.distributed.barrier()
        autoresume.init()
        torch.distributed.barrier()


def _set_random_seed(seed_, data_parallel_random_init=False):
    """Set random seed for reproducability."""
    if seed_ is not None and seed_ > 0:
        # Ensure that different pipeline MP stages get different seeds.
        seed = seed_ + (100 * mpu.get_pipeline_model_parallel_rank())
        # Ensure different data parallel ranks get different seeds
        if data_parallel_random_init:
            seed = seed + (10 * mpu.get_data_parallel_rank())
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.device_count() > 0:
            mpu.model_parallel_cuda_manual_seed(seed)
    else:
        raise ValueError('Seed ({}) should be a positive integer.'.format(seed))


def write_args_to_tensorboard():
    """Write arguments to tensorboard."""
    args = get_args()
    writer = get_tensorboard_writer()
    if writer:
        for arg in vars(args):
            writer.add_text(arg, str(getattr(args, arg)),
                            global_step=args.iteration)

def init_wandb():
    args = get_args()
    if args.rank == (args.world_size - 1):
        if not (args.wandb_entity_name and args.wandb_project_name):
            print('> Skipping wandb init ...', flush=True)
            return
        wandb.init(
            name=os.path.basename(args.save),
            entity=args.wandb_entity_name,
            project=args.wandb_project_name,
            group="mini_cluster",
            config=args
        )


def set_jit_fusion_options():
    """Set PyTorch JIT layer fusion options."""
    # flags required to enable jit fusion kernels
    TORCH_MAJOR = int(torch.__version__.split('.')[0])
    TORCH_MINOR = int(torch.__version__.split('.')[1])
    if (TORCH_MAJOR > 1) or (TORCH_MAJOR == 1 and TORCH_MINOR >= 10):
        # nvfuser
        torch._C._jit_set_profiling_executor(True)
        torch._C._jit_set_profiling_mode(True)
        torch._C._jit_override_can_fuse_on_cpu(False)
        torch._C._jit_override_can_fuse_on_gpu(False)
        torch._C._jit_set_texpr_fuser_enabled(False)
        torch._C._jit_set_nvfuser_enabled(True)
        torch._C._debug_set_autodiff_subgraph_inlining(False)
    else:
        # legacy pytorch fuser
        torch._C._jit_set_profiling_mode(False)
        torch._C._jit_set_profiling_executor(False)
        torch._C._jit_override_can_fuse_on_cpu(True)
        torch._C._jit_override_can_fuse_on_gpu(True)

    _warmup_jit_function()


def _warmup_jit_function():
    """ Compilie JIT functions before the main training steps """
    args = get_args()
    if args.bf16:
        dtype = torch.bfloat16
    elif args.fp16:
        dtype = torch.float16
    else:
        dtype = torch.float32

    # Warmup fused bias+gelu
    bias = torch.rand(args.ffn_hidden_size // args.tensor_model_parallel_size,
                      dtype=dtype, device='cuda')
    input = torch.rand((args.seq_length, args.micro_batch_size,
                        args.ffn_hidden_size // args.tensor_model_parallel_size),
                       dtype=dtype, device='cuda')
    # Warmup JIT fusions with the input grad_enable state of both forward
    # prop and recomputation
    for bias_grad, input_grad in zip([True, True], [False, True]):
        bias.requires_grad, input.requires_grad = bias_grad, input_grad
        for _ in range(5):
            output = bias_gelu(bias, input)
    del bias, input, output

    # Warmup fused bias+dropout+add
    if args.sequence_parallel:
        seq_length = args.seq_length // mpu.get_tensor_model_parallel_world_size()
    else:
        seq_length = args.seq_length
    input = torch.rand((seq_length, args.micro_batch_size, args.hidden_size),
                       dtype=dtype, device='cuda')
    residual = torch.rand((seq_length, args.micro_batch_size, args.hidden_size),
                          dtype=dtype, device='cuda')
    bias = torch.rand((args.hidden_size), dtype=dtype, device='cuda').expand_as(residual)
    dropout_rate = 0.1
    # Warmup JIT fusions with the input grad_enable state of both forward
    # prop and recomputation
    for input_grad, bias_grad, residual_grad in zip([False, True], [True, True], [True, True]):
        input.requires_grad = input_grad
        bias.requires_grad = bias_grad
        residual.requires_grad = residual_grad
        for _ in range(5):
            output = bias_dropout_add_fused_train(input, bias, residual, dropout_rate)
    del bias, input, residual, output
    torch.cuda.empty_cache()

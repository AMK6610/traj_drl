# coding=utf-8
# Copyright 2020 The Google Research Authors.
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

"""All functions and modules related to model definition.
"""

import torch
import torch.nn as nn
from .. import sde_lib
import numpy as np
import logging

_MODELS = {}


def register_model(cls=None, *, name=None):
    """A decorator for registering model classes."""

    def _register(cls):
        if name is None:
            local_name = cls.__name__
        else:
            local_name = name
        if local_name in _MODELS:
            raise ValueError(f'Already registered model with name: {local_name}')
        _MODELS[local_name] = cls
        return cls

    if cls is None:
        return _register
    else:
        return _register(cls)


def get_model(name):
    return _MODELS[name]


def get_sigmas(config):
    """Get sigmas --- the set of noise levels for SMLD from config files.
    Args:
      config: A ConfigDict object parsed from the config file
    Returns:
      sigmas: a jax numpy arrary of noise levels
    """
    sigmas = np.exp(
        np.linspace(np.log(config.model.sigma_max), np.log(config.model.sigma_min), config.model.num_scales))

    return sigmas


def get_ddpm_params(config):
    """Get betas and alphas --- parameters used in the original DDPM paper."""
    num_diffusion_timesteps = 1000
    # parameters need to be adapted if number of time steps differs from 1000
    beta_start = config.model.beta_min / config.model.num_scales
    beta_end = config.model.beta_max / config.model.num_scales
    betas = np.linspace(beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64)

    alphas = 1. - betas
    alphas_cumprod = np.cumprod(alphas, axis=0)
    sqrt_alphas_cumprod = np.sqrt(alphas_cumprod)
    sqrt_1m_alphas_cumprod = np.sqrt(1. - alphas_cumprod)

    return {
        'betas': betas,
        'alphas': alphas,
        'alphas_cumprod': alphas_cumprod,
        'sqrt_alphas_cumprod': sqrt_alphas_cumprod,
        'sqrt_1m_alphas_cumprod': sqrt_1m_alphas_cumprod,
        'beta_min': beta_start * (num_diffusion_timesteps - 1),
        'beta_max': beta_end * (num_diffusion_timesteps - 1),
        'num_diffusion_timesteps': num_diffusion_timesteps
    }


def create_model(config):
    """Create the score model."""
    model_name = config.model.name
    score_model = get_model(model_name)(config)
    # x = torch.zeros((2, 2)).to(config.device)
    score_model = score_model.to(config.device)
    score_model = torch.nn.DataParallel(score_model)
    return score_model


def get_model_fn(model, train=False):
    """Create a function to give the output of the score-based model.

    Args:
      model: The score model.
      train: `True` for training and `False` for evaluation.

    Returns:
      A model function.
    """

    def model_fn(x, labels, x0=None, t=None, e=None):
        """Compute the output of the score-based model.

        Args:
          x: A mini-batch of input data.
          labels: A mini-batch of conditioning variables for time steps. Should be interpreted differently
            for different models.

        Returns:
          A tuple of (model output, new mutable states)
        """
        if not train:
            model.eval()
            return model(x, labels, x0=x0, t=t, e=e)
        else:
            model.train()
            return model(x, labels, x0=x0, t=t, e=e)

    return model_fn


def get_score_fn(sde, model, train=False, continuous=False):
    """Wraps `score_fn` so that the model output corresponds to a real time-dependent score function.

    Args:
      sde: An `sde_lib.SDE` object that represents the forward SDE.
      model: A score model.
      train: `True` for training and `False` for evaluation.
      continuous: If `True`, the score-based model is expected to directly take continuous time steps.

    Returns:
      A score function.
    """
    model_fn = get_model_fn(model, train=train)

    if isinstance(sde, sde_lib.VPSDE) or isinstance(sde, sde_lib.subVPSDE):
        def score_fn(x, t):
            # Scale neural network output by standard deviation and flip sign
            if continuous or isinstance(sde, sde_lib.subVPSDE):
                # For VP-trained models, t=0 corresponds to the lowest noise level
                # The maximum value of time embedding is assumed to 999 for
                # continuously-trained models.
                labels = t * 999
                score = model_fn(x, labels)
                std = sde.marginal_prob(torch.zeros_like(x), t)[1]
            else:
                # For VP-trained models, t=0 corresponds to the lowest noise level
                labels = t * (sde.N - 1)
                score = model_fn(x, labels)
                std = sde.sqrt_1m_alphas_cumprod.to(labels.device)[labels.long()]

            score = -score / std[:, None, None, None]
            return score

    elif isinstance(sde, sde_lib.VESDE):
        def score_fn(x, t, x0=None, e=None):
            if continuous:
                labels = sde.marginal_prob(torch.zeros_like(x), t)[1]
            else:
                # For VE-trained models, t=0 corresponds to the highest noise level
                labels = sde.T - t
                labels *= sde.N - 1
                labels = torch.round(labels).long()

            score = model_fn(x, labels, x0=x0, t=t, e=e)
            return {k: v.detach() if not train and hasattr(v, 'detach') else v for k, v in score.items()}
            if not train:
                for v in score.values():
                    if hasattr(v, 'detach'):
                        v.detach_()
            return score  # {k: v.detach() if not train and hasattr(v, 'detach') else v for k, v in score.items()}

    else:
        raise NotImplementedError(f"SDE class {sde.__class__.__name__} not yet supported.")

    return score_fn


def to_flattened_numpy(x):
    """Flatten a torch tensor `x` and convert it to numpy."""
    return x.detach().cpu().numpy().reshape((-1,))


def from_flattened_numpy(x, shape):
    """Form a torch tensor with the given `shape` from a flattened numpy array `x`."""
    return torch.from_numpy(x.reshape(shape))

def inverse_softplus(x, beta=1.0):
    """Inverse of the softplus function"""
    return 1 / beta * np.log(np.exp(beta * x) - 1.0)

def get_activation(key):
    """Utility function that returns an activation function given its name"""

    if key == "relu":
        return nn.ReLU()
    elif key == "leaky_relu":
        return nn.LeakyReLU()
    elif key == "softmax":
        return nn.Softmax(dim=1)
    else:
        raise ValueError(f"Unknown activation {key}")

def make_mlp(features, activation="relu", final_activation=None, initial_activation=None):
    """Utility function that constructs a simple MLP from specs"""

    if len(features) >= 2:
        layers = []

        if initial_activation is not None:
            layers.append(get_activation(initial_activation))

        for in_, out in zip(features[:-2], features[1:-1]):
            layers.append(nn.Linear(in_, out))
            layers.append(get_activation(activation))

        layers.append(nn.Linear(features[-2], features[-1]))
        if final_activation is not None:
            layers.append(get_activation(final_activation))

        net = nn.Sequential(*layers)

    else:
        net = nn.Identity()

    return net
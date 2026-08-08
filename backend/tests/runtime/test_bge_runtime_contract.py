"""BGE embedding runtime contract.

The InsightForge backend image contract is **CPU-only PyTorch**: the frozen BGE
runtime (sentence-transformers 4.1.0 / transformers 4.57.6 / tokenizers 0.22.2)
runs on the CPU build of torch 2.13.0. No CUDA runtime must be present
(``torch.version.cuda is None`` and ``torch.cuda.is_available() is False``), and
the installed versions must match the pyproject pins exactly.

These assertions run both in the local test suite and inside the built image,
so a packaging regression (e.g. ``pip install .`` resolving CUDA torch from the
default PyPI index) fails loudly instead of silently shipping GPU wheels.
"""

import torch


def test_torch_is_frozen_public_version():
    assert torch.__version__.split("+")[0] == "2.13.0"


def test_torch_has_no_cuda_runtime():
    assert torch.version.cuda is None
    assert torch.cuda.is_available() is False


def test_sentence_transformers_is_frozen():
    import sentence_transformers

    assert sentence_transformers.__version__ == "4.1.0"


def test_transformers_is_frozen():
    import transformers

    assert transformers.__version__ == "4.57.6"


def test_tokenizers_is_frozen():
    import tokenizers

    assert tokenizers.__version__ == "0.22.2"

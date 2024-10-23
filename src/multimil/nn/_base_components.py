from math import ceil
import numpy as np
import pandas as pd
import scipy
import torch
from matplotlib import pyplot as plt

def calculate_size_factor(adata, size_factor_key, rna_indices_end) -> str:
    """Calculate size factors.

    Parameters
    ----------
    adata
        Annotated data object.
    size_factor_key
        Key in `adata.obs` where size factors are stored.
    rna_indices_end
        Index of the last RNA feature in the data.

    Returns
    -------
    Size factor key.
    """
    # TODO check that organize_multimodal_anndatas was run, i.e. that .uns['modality_lengths'] was added, needed for q2r
    if size_factor_key is not None and rna_indices_end is not None:
        raise ValueError(
            "Only one of [`size_factor_key`, `rna_indices_end`] can be specified, but both are not `None`."
        )
    # TODO change to when both are None and data in unimodal, use all input features to calculate the size factors, add warning
    if size_factor_key is None and rna_indices_end is None:
        raise ValueError("One of [`size_factor_key`, `rna_indices_end`] has to be specified, but both are `None`.")

    if size_factor_key is not None:
        return size_factor_key
    if rna_indices_end is not None:
        adata_rna = adata[:, :rna_indices_end].copy()
        if scipy.sparse.issparse(adata.X):
            adata.obs.loc[:, "size_factors"] = adata_rna.X.toarray().sum(1).T.tolist()
        else:
            adata.obs.loc[:, "size_factors"] = adata_rna.X.sum(1).T.tolist()
        return "size_factors"

def select_covariates(covs, prediction_idx, n_samples_in_batch) -> torch.Tensor:
    """Select prediction covariates from all covariates.

    Parameters
    ----------
    covs : torch.Tensor
        Covariates.
    prediction_idx : list
        Index of predictions.
    n_samples_in_batch : int
        Number of samples in the batch.

    Returns
    -------
    torch.Tensor
        Prediction covariates.
    """
    if len(prediction_idx) > 0:
        covs = torch.index_select(covs, 1, torch.tensor(prediction_idx))
        covs = covs.view(n_samples_in_batch, -1, len(prediction_idx))[:, 0, :]
    else:
        covs = torch.tensor([])
    return covs

def prep_minibatch(covs, sample_batch_size) -> tuple[int, int]:
    """Prepare minibatch.

    Parameters
    ----------
    covs : torch.Tensor
        Covariates.
    sample_batch_size : int
        Sample batch size.

    Returns
    -------
    tuple[int, int]
        Batch size and number of samples in the batch.
    """
    batch_size = covs.shape[0]
    n_samples_in_batch = 1 if batch_size % sample_batch_size != 0 else batch_size // sample_batch_size
    return batch_size, n_samples_in_batch

def plt_plot_losses(history, loss_names, save):
    """Plot losses.

    Parameters
    ----------
    history : list
        History of losses.
    loss_names : list
        Loss names to plot.
    save : str
        Path to save the plot.

    Returns
    -------
    None
    """
    df = pd.concat(history, axis=1)
    df.columns = df.columns.droplevel(-1)
    df["epoch"] = df.index

    nrows = ceil(len(loss_names) / 2)

    plt.figure(figsize=(15, 5 * nrows))

    for i, name in enumerate(loss_names):
        plt.subplot(nrows, 2, i + 1)
        plt.plot(df["epoch"], df[name + "_train"], ".-", label=name + "_train")
        plt.plot(df["epoch"], df[name + "_validation"], ".-", label=name + "_validation")
        plt.xlabel("epoch")
        plt.legend()
    if save is not None:
        plt.savefig(save, bbox_inches="tight")

import torch
from torch import nn
from torch.nn import functional as F
from ..nn import *

from scvi.module.base import BaseModuleClass, LossRecorder, auto_move_data
from scvi import _CONSTANTS

from torch.distributions import Normal
from torch.distributions import kl_divergence as kl
from ._multivae_torch import MultiVAETorch


class Aggregator(nn.Module):
    def __init__(
        self,
        n_input=None,
        scoring="sum",
        attn_dim=16,  # D
        patient_batch_size=None,
        scale=False,
        attention_dropout=True,
        drop_attn=False,
        dropout=0.2,
        n_layers_mlp_attn=1,
        n_hidden_mlp_attn=16,
    ):
        super().__init__()

        self.scoring = scoring
        self.patient_batch_size = patient_batch_size
        self.scale = scale

        if self.scoring == "attn":
            self.attn_dim = (
                attn_dim  # attn dim from https://arxiv.org/pdf/1802.04712.pdf
            )
            self.attention = nn.Sequential(
                nn.Linear(n_input, self.attn_dim),
                nn.Tanh(),
                nn.Dropout(dropout) if attention_dropout else nn.Identity(),
                nn.Linear(self.attn_dim, 1, bias=False),
            )
        elif self.scoring == "gated_attn":
            self.attn_dim = attn_dim
            self.attention_V = nn.Sequential(
                nn.Linear(n_input, self.attn_dim),
                nn.Tanh(),
                nn.Dropout(dropout) if attention_dropout else nn.Identity(),
            )

            self.attention_U = nn.Sequential(
                # orthogonal(nn.Linear(z_dim, self.attn_dim)),
                nn.Linear(n_input, self.attn_dim),
                nn.Sigmoid(),
                nn.Dropout(dropout) if attention_dropout else nn.Identity(),
            )

            self.attention_weights = nn.Linear(self.attn_dim, 1, bias=False)

        elif self.scoring == "MLP":

            if n_layers_mlp_attn == 1:
                self.attention = nn.Linear(n_input, 1)
            else:
                self.attention = nn.Sequential(
                    MLP(
                        n_input,
                        n_hidden_mlp_attn,
                        n_layers=n_layers_mlp_attn - 1,
                        n_hidden=n_hidden_mlp_attn,
                        dropout_rate=dropout,
                    ),
                    nn.Linear(n_hidden_mlp_attn, 1),
                )
        self.dropout_attn = nn.Dropout(dropout) if drop_attn else nn.Identity()

    def forward(self, x):
        if self.scoring == "sum":
            return torch.sum(x, dim=0)  # z_dim

        elif self.scoring == "attn":
            # from https://github.com/AMLab-Amsterdam/AttentionDeepMIL/blob/master/model.py (accessed 16.09.2021)
            self.A = self.attention(x)  # Nx1
            self.A = torch.transpose(self.A, -1, -2)  # 1xN
            self.A = F.softmax(self.A, dim=-1)  # softmax over N

        elif self.scoring == "gated_attn":
            # from https://github.com/AMLab-Amsterdam/AttentionDeepMIL/blob/master/model.py (accessed 16.09.2021)
            A_V = self.attention_V(x)  # NxD
            A_U = self.attention_U(x)  # NxD
            self.A = self.attention_weights(
                A_V * A_U
            )  # element wise multiplication # Nx1
            self.A = torch.transpose(self.A, -1, -2)  # 1xN
            self.A = F.softmax(self.A, dim=-1)  # softmax over N

        elif self.scoring == "MLP":
            self.A = self.attention(x)  # N
            self.A = torch.transpose(self.A, -1, -2)
            self.A = F.softmax(self.A, dim=-1)

        if self.scale:
            self.A = self.A * self.A.shape[-1] / self.patient_batch_size

        self.A = self.dropout_attn(self.A)

        return torch.bmm(self.A, x).squeeze(dim=1)  # z_dim


class MultiVAETorch_MIL(BaseModuleClass):
    def __init__(
        self,
        modality_lengths,
        condition_encoders=False,
        condition_decoders=True,
        normalization="layer",
        z_dim=16,
        h_dim=32,
        losses=[],
        dropout=0.2,
        cond_dim=16,
        kernel_type="gaussian",
        loss_coefs=[],
        num_groups=1,
        integrate_on_idx=None,
        n_layers_encoders=[],
        n_layers_decoders=[],
        n_layers_shared_decoder: int = 1,
        n_hidden_encoders=[],
        n_hidden_decoders=[],
        n_hidden_shared_decoder: int = 32,
        add_shared_decoder=True,
        patient_idx=None,
        num_classes=[],  # number of classes for each of the classification task
        scoring="gated_attn",
        attn_dim=16,
        cat_covariate_dims=[],
        cont_covariate_dims=[],
        cont_cov_type="logsigm",
        n_layers_cell_aggregator=1,
        n_layers_cov_aggregator=1,
        n_layers_classifier=1,
        n_layers_mlp_attn=1,
        n_layers_cont_embed=1,
        n_layers_regressor=1,
        n_hidden_regressor=16,
        n_hidden_cell_aggregator=16,
        n_hidden_cov_aggregator=16,
        n_hidden_classifier=16,
        n_hidden_mlp_attn=16,
        n_hidden_cont_embed=16,
        class_loss_coef=1.0,
        regression_loss_coef=1.0,
        reg_coef=1,
        add_patient_to_classifier=False,
        hierarchical_attn=True,
        patient_batch_size=128,
        regularize_cell_attn=False,
        regularize_cov_attn=False,
        regularize_vae=False,
        attention_dropout=True,
        class_idx=[],  # which indices in cat covariates to do classification on, i.e. exclude from inference
        ord_idx=[],  # which indices in cat covariates to do ordinal regression on and also exclude from inference
        reg_idx=[],  # which indices in cont covariates to do regression on and also exclude from inference
        drop_attn=False,
        mmd="latent",
    ):
        super().__init__()

        self.vae = MultiVAETorch(
            modality_lengths=modality_lengths,
            condition_encoders=condition_encoders,
            condition_decoders=condition_decoders,
            normalization=normalization,
            z_dim=z_dim,
            h_dim=h_dim,
            losses=losses,
            dropout=dropout,
            cond_dim=cond_dim,
            kernel_type=kernel_type,
            loss_coefs=loss_coefs,
            num_groups=num_groups,
            integrate_on_idx=integrate_on_idx,
            cat_covariate_dims=cat_covariate_dims,  # only the actual categorical covs are considered here
            cont_covariate_dims=cont_covariate_dims,  # only the actual cont covs are considered here
            cont_cov_type=cont_cov_type,
            n_layers_encoders=n_layers_encoders,
            n_layers_decoders=n_layers_decoders,
            n_layers_shared_decoder=n_layers_shared_decoder,
            n_layers_cont_embed=n_layers_cont_embed,
            n_hidden_encoders=n_hidden_encoders,
            n_hidden_decoders=n_hidden_decoders,
            n_hidden_shared_decoder=n_hidden_shared_decoder,
            n_hidden_cont_embed=n_hidden_cont_embed,
            add_shared_decoder=add_shared_decoder,
            mmd=mmd,
        )

        self.integrate_on_idx = integrate_on_idx
        self.class_loss_coef = class_loss_coef
        self.regression_loss_coef = regression_loss_coef
        self.reg_coef = reg_coef
        self.add_patient_to_classifier = add_patient_to_classifier
        self.patient_idx = patient_idx
        self.hierarchical_attn = hierarchical_attn
        self.patient_batch_size = patient_batch_size
        self.regularize_cell_attn = regularize_cell_attn
        self.regularize_cov_attn = regularize_cov_attn
        self.regularize_vae = regularize_vae

        self.cat_cov_idx = torch.tensor(
            list(
                set(range(len(class_idx) + len(ord_idx) + len(cat_covariate_dims)))
                .difference(set(class_idx))
                .difference(set(ord_idx))
            )
        )
        self.cont_cov_idx = torch.tensor(
            list(
                set(range(len(reg_idx) + len(cont_covariate_dims))).difference(
                    set(reg_idx)
                )
            )
        )

        self.class_idx = torch.tensor(class_idx)
        self.ord_idx = torch.tensor(ord_idx)
        self.reg_idx = torch.tensor(reg_idx)

        self.cond_dim = cond_dim
        self.cell_level_aggregator = nn.Sequential(
            MLP(
                z_dim,
                cond_dim,
                n_layers=n_layers_cell_aggregator,
                n_hidden=n_hidden_cell_aggregator,
                dropout_rate=dropout,
            ),
            Aggregator(
                n_input=cond_dim,
                scoring=scoring,
                attn_dim=attn_dim,
                patient_batch_size=patient_batch_size,
                scale=True,
                attention_dropout=attention_dropout,
                drop_attn=drop_attn,
                dropout=dropout,
                n_layers_mlp_attn=n_layers_mlp_attn,
                n_hidden_mlp_attn=n_hidden_mlp_attn,
            ),
        )
        if hierarchical_attn:
            self.cov_level_aggregator = nn.Sequential(
                MLP(
                    cond_dim,
                    cond_dim,
                    n_layers=n_layers_cov_aggregator,
                    n_hidden=n_hidden_cov_aggregator,
                    dropout_rate=dropout,
                ),
                Aggregator(
                    n_input=cond_dim,
                    scoring=scoring,
                    attn_dim=attn_dim,
                    attention_dropout=attention_dropout,
                    drop_attn=drop_attn,
                    dropout=dropout,
                    n_layers_mlp_attn=n_layers_mlp_attn,
                    n_hidden_mlp_attn=n_hidden_mlp_attn,
                ),
            )

        self.classifiers = torch.nn.ModuleList()
        for num in num_classes:
            if n_layers_classifier == 1:
                self.classifiers.append(nn.Linear(cond_dim, num))
            else:
                self.classifiers.append(
                    nn.Sequential(
                        MLP(
                            cond_dim,
                            n_hidden_classifier,
                            n_layers=n_layers_classifier - 1,
                            n_hidden=n_hidden_classifier,
                            dropout_rate=dropout,
                        ),
                        nn.Linear(n_hidden_classifier, num),
                    )
                )

        self.regressors = torch.nn.ModuleList()
        for _ in range(
            len(self.ord_idx) + len(self.reg_idx)
        ):  # one head per standard regression and one per ordinal regression
            if n_layers_regressor == 1:
                self.regressors.append(nn.Linear(cond_dim, 1))
            else:
                self.regressors.append(
                    nn.Sequential(
                        MLP(
                            cond_dim,
                            n_hidden_regressor,
                            n_layers=n_layers_regressor - 1,
                            n_hidden=n_hidden_regressor,
                            dropout_rate=dropout,
                        ),
                        nn.Linear(n_hidden_regressor, 1),
                    )
                )

    def _get_inference_input(self, tensors):
        x = tensors[_CONSTANTS.X_KEY]

        cont_key = _CONSTANTS.CONT_COVS_KEY
        cont_covs = tensors[cont_key] if cont_key in tensors.keys() else None

        cat_key = _CONSTANTS.CAT_COVS_KEY
        cat_covs = tensors[cat_key] if cat_key in tensors.keys() else None

        input_dict = dict(x=x, cat_covs=cat_covs, cont_covs=cont_covs)
        return input_dict

    def _get_generative_input(self, tensors, inference_outputs):
        z_joint = inference_outputs["z_joint"]

        cont_key = _CONSTANTS.CONT_COVS_KEY
        cont_covs = tensors[cont_key] if cont_key in tensors.keys() else None

        cat_key = _CONSTANTS.CAT_COVS_KEY
        cat_covs = tensors[cat_key] if cat_key in tensors.keys() else None

        return dict(z_joint=z_joint, cat_covs=cat_covs, cont_covs=cont_covs)

    @auto_move_data
    def inference(self, x, cat_covs, cont_covs):
        # vae part
        if len(self.cont_cov_idx) > 0:
            cont_covs = torch.index_select(
                cont_covs, 1, self.cont_cov_idx.to(self.device)
            )
        if len(self.cat_cov_idx) > 0:
            cat_covs = torch.index_select(cat_covs, 1, self.cat_cov_idx.to(self.device))

        inference_outputs = self.vae.inference(x, cat_covs, cont_covs)
        z_joint = inference_outputs["z_joint"]

        # MIL part
        batch_size = x.shape[0]

        idx = list(
            range(self.patient_batch_size, batch_size, self.patient_batch_size)
        )  # or depending on model.train() and model.eval() ???
        if (
            batch_size % self.patient_batch_size != 0
        ):  # can only happen during inference for last batches for each patient
            idx = []

        zs = torch.tensor_split(z_joint, idx, dim=0)
        zs = torch.stack(zs, dim=0)
        zs = self.cell_level_aggregator(zs)  # num of bags in batch x cond_dim

        predictions = []

        # TODO: fix for the case that there are no covatiates
        if self.hierarchical_attn:
            add_covariate = lambda i: self.add_patient_to_classifier or (
                not self.add_patient_to_classifier and i != self.patient_idx
            )
            if len(self.vae.cat_covariate_embeddings) > 0:
                cat_embedds = torch.cat(
                    [
                        cat_covariate_embedding(covariate.long())
                        for covariate, cat_covariate_embedding, i in zip(
                            cat_covs.T,
                            self.vae.cat_covariate_embeddings,
                            self.cat_cov_idx,
                        )
                        if add_covariate(i)
                    ],
                    dim=-1,
                )
            else:
                cat_embedds = torch.Tensor().to(self.device)  # so cat works later
            if self.vae.n_cont_cov > 0:
                if (
                    cont_covs.shape[-1] != self.vae.n_cont_cov
                ):  # get rid of size_factors
                    raise RuntimeError("cont_covs.shape[-1] != self.vae.n_cont_cov")
                    # cont_covs = cont_covs[:, 0:self.vae.n_cont_cov]
                cont_embedds = self.vae.compute_cont_cov_embeddings_(cont_covs)
            else:
                cont_embedds = torch.Tensor().to(self.device)

            cov_embedds = torch.cat([cat_embedds, cont_embedds], dim=-1)

            cov_embedds = torch.tensor_split(cov_embedds, idx)
            cov_embedds = [embed[0] for embed in cov_embedds]
            cov_embedds = torch.stack(cov_embedds, dim=0)

            aggr_bag_level = torch.cat([zs, cov_embedds], dim=-1)
            aggr_bag_level = torch.split(aggr_bag_level, self.cond_dim, dim=-1)
            aggr_bag_level = torch.stack(
                aggr_bag_level, dim=1
            )  # num of bags in batch x num of cat covs + num of cont covs + 1 (molecular information) x cond_dim
            aggr_bag_level = self.cov_level_aggregator(aggr_bag_level)

            predictions.extend(
                [classifier(aggr_bag_level) for classifier in self.classifiers]
            )  # each one num of bags in batch x num of classes
            predictions.extend(
                [regressor(aggr_bag_level) for regressor in self.regressors]
            )
        else:
            predictions.extend([classifier(zs) for classifier in self.classifiers])
            predictions.extend([regressor(zs) for regressor in self.regressors])

        inference_outputs.update(
            {"predictions": predictions}
        )  # predictions are a list as they can have different number of classes
        return inference_outputs  # z_joint, mu, logvar, predictions

    @auto_move_data
    def generative(self, z_joint, cat_covs, cont_covs):
        if len(self.cont_cov_idx) > 0:
            cont_covs = torch.index_select(
                cont_covs, 1, self.cont_cov_idx.to(self.device)
            )
        if len(self.cat_cov_idx) > 0:
            cat_covs = torch.index_select(cat_covs, 1, self.cat_cov_idx.to(self.device))
        return self.vae.generative(z_joint, cat_covs, cont_covs)

    def orthogonal_regularization(self, weights, axis=0):
        loss = torch.tensor(0.0).to(self.device)
        for weight in weights:
            if axis == 1:
                weight = weight.T
            dim = weight.shape[1]
            loss += torch.sqrt(
                torch.sum(
                    (
                        torch.matmul(weight.T, weight) - torch.eye(dim).to(self.device)
                    ).pow(2)
                )
            )
        return loss

    def loss(
        self, tensors, inference_outputs, generative_outputs, kl_weight: float = 1.0
    ):
        x = tensors[_CONSTANTS.X_KEY]

        cont_key = _CONSTANTS.CONT_COVS_KEY
        cont_covs = tensors[cont_key] if cont_key in tensors.keys() else None

        cat_key = _CONSTANTS.CAT_COVS_KEY
        cat_covs = tensors[cat_key] if cat_key in tensors.keys() else None

        if self.integrate_on_idx:
            integrate_on = cat_covs[:, self.integrate_on_idx]
        else:
            integrate_on = torch.zeros(x.shape[0], 1).to(self.device)

        size_factor = cont_covs[:, -1]  # always last

        # MIL classification loss
        batch_size = x.shape[0]
        idx = list(
            range(self.patient_batch_size, batch_size, self.patient_batch_size)
        )  # or depending on model.train() and model.eval() ???
        if (
            batch_size % self.patient_batch_size != 0
        ):  # can only happen during inference for last batches for each patient
            idx = []

        # TODO in a function
        if len(self.reg_idx) > 0:
            regression = torch.index_select(cont_covs, 1, self.reg_idx.to(self.device))
            regression = regression.view(len(idx) + 1, -1, len(self.reg_idx))[:, 0, :]
        if len(self.cont_cov_idx) > 0:
            cont_covs = torch.index_select(
                cont_covs, 1, self.cont_cov_idx.to(self.device)
            )

        if len(self.ord_idx) > 0:
            ordinal_regression = torch.index_select(
                cat_covs, 1, self.ord_idx.to(self.device)
            )
            ordinal_regression = ordinal_regression.view(
                len(idx) + 1, -1, len(self.ord_idx)
            )[:, 0, :]
        if len(self.class_idx) > 0:
            classification = torch.index_select(
                cat_covs, 1, self.class_idx.to(self.device)
            )
            classification = classification.view(len(idx) + 1, -1, len(self.class_idx))[
                :, 0, :
            ]
        if len(self.cat_cov_idx) > 0:
            cat_covs = torch.index_select(cat_covs, 1, self.cat_cov_idx.to(self.device))

        rs = generative_outputs["rs"]
        mu = inference_outputs["mu"]
        logvar = inference_outputs["logvar"]
        z_joint = inference_outputs["z_joint"]
        predictions = inference_outputs[
            "predictions"
        ]  # list, first from classifiers, then from regressors
        z_marginal = inference_outputs["z_marginal"]

        xs = torch.split(
            x, self.vae.input_dims, dim=-1
        )  # list of tensors of len = n_mod, each tensor is of shape batch_size x mod_input_dim
        masks = [x.sum(dim=1) > 0 for x in xs]

        kl_loss = kl(Normal(mu, torch.sqrt(torch.exp(logvar))), Normal(0, 1)).sum(dim=1)
        integ_loss = (
            torch.tensor(0.0)
            if self.vae.loss_coefs["integ"] == 0
            else self.vae.calc_integ_loss(z_joint, integrate_on)
        )

        recon_loss, modality_recon_losses = self.vae.calc_recon_loss(
            xs,
            rs,
            self.vae.losses,
            integrate_on,
            size_factor,
            self.vae.loss_coefs,
            masks,
        )

        if self.vae.loss_coefs["integ"] == 0:
            integ_loss = torch.tensor(0.0).to(self.device)
        else:
            integ_loss = torch.tensor(0.0).to(self.device)
            if self.vae.mmd == "latent" or self.vae.mmd == "both":
                integ_loss += self.vae.calc_integ_loss(z_joint, integrate_on).to(
                    self.device
                )
            if self.vae.mmd == "marginal" or self.vae.mmd == "both":
                for i in range(len(masks)):
                    for j in range(i + 1, len(masks)):
                        idx_where_to_calc_mmd = torch.eq(
                            masks[i] == masks[j],
                            torch.eq(masks[i], torch.ones_like(masks[i])),
                        )
                        if (
                            idx_where_to_calc_mmd.any()
                        ):  # if need to calc mmd for a group between modalities
                            marginal_i = z_marginal[:, i, :][idx_where_to_calc_mmd]
                            marginal_j = z_marginal[:, j, :][idx_where_to_calc_mmd]
                            marginals = torch.cat([marginal_i, marginal_j])
                            modalities = torch.cat(
                                [
                                    torch.Tensor([i] * marginal_i.shape[0]),
                                    torch.Tensor([j] * marginal_j.shape[0]),
                                ]
                            ).to(self.device)

                            integ_loss += self.vae.calc_integ_loss(
                                marginals, modalities
                            ).to(self.device)

                for i in range(len(masks)):
                    marginal_i = z_marginal[:, i, :]
                    marginal_i = marginal_i[masks[i]]
                    group_marginal = integrate_on[masks[i]]
                    integ_loss += self.vae.calc_integ_loss(
                        marginal_i, group_marginal
                    ).to(self.device)

        cycle_loss = (
            torch.tensor(0.0).to(self.device)
            if self.vae.loss_coefs["cycle"] == 0
            else self.vae.calc_cycle_loss(
                xs,
                z_joint,
                integrate_on,
                masks,
                self.vae.losses,
                size_factor,
                self.vae.loss_coefs,
            )
        )

        classification_loss = torch.tensor(0.0).to(self.device)
        accuracies = []
        for i in range(len(self.class_idx)):
            classification_loss += F.cross_entropy(
                predictions[i], classification[:, i].long()
            )  # assume same in the batch
            accuracies.append(
                torch.sum(
                    torch.eq(torch.argmax(predictions[i], dim=-1), classification[:, i])
                )
                / classification[:, i].shape[0]
            )
        accuracy = torch.sum(torch.tensor(accuracies)) / len(accuracies)

        regression_loss = torch.tensor(0.0).to(self.device)
        for i in range(len(self.ord_idx)):
            regression_loss += F.mse_loss(
                predictions[len(self.class_idx) + i].squeeze(), ordinal_regression[:, i]
            )
        for i in range(len(self.reg_idx)):
            regression_loss += F.mse_loss(
                predictions[len(self.class_idx) + len(self.ord_idx) + i].squeeze(),
                regression[:, i],
            )

        # what to regularize:
        weights = []
        if self.regularize_cov_attn:
            weights.append(self.cov_level_aggregator[1].attention_U[0].weight)
            weights.append(self.cov_level_aggregator[1].attention_V[0].weight)
        if self.regularize_cell_attn:
            weights.append(self.cell_level_aggregator[1].attention_U[0].weight)
            weights.append(self.cell_level_aggregator[1].attention_V[0].weight)

        # TODO: fix if other layers
        if self.regularize_vae:
            weights.append(self.vae.shared_decoder.encoder.fc_layers[0][0].weight)
            weights.append(self.vae.encoder_0.encoder.fc_layers[0][0].weight)
            weights.append(self.vae.encoder_1.encoder.fc_layers[0][0].weight)
            weights.append(self.vae.decoder_0.decoder.encoder.fc_layers[0][0].weight)
            weights.append(self.vae.decoder_1.decoder.encoder.fc_layers[0][0].weight)

        reg_loss = self.orthogonal_regularization(weights)

        loss = torch.mean(
            self.vae.loss_coefs["recon"] * recon_loss
            + self.vae.loss_coefs["kl"] * kl_loss
            + self.vae.loss_coefs["integ"] * integ_loss
            + self.vae.loss_coefs["cycle"] * cycle_loss
            + self.class_loss_coef * classification_loss
            + self.regression_loss_coef * regression_loss
            + self.reg_coef * reg_loss
        )

        reconst_losses = dict(recon_loss=recon_loss)

        return LossRecorder(
            loss,
            reconst_losses,
            self.vae.loss_coefs["kl"] * kl_loss,
            kl_global=torch.tensor(0.0),
            integ_loss=integ_loss,
            cycle_loss=cycle_loss,
            class_loss=classification_loss,
            accuracy=accuracy,
            reg_loss=reg_loss,
            regression_loss=regression_loss,
        )

    # TODO ??
    @torch.no_grad()
    def sample(self, tensors):
        with torch.no_grad():
            (
                _,
                generative_outputs,
            ) = self.forward(tensors, compute_loss=False)

        return generative_outputs["rs"]

# -*- coding: utf-8 -*-
"""Deep Anomaly Detection on Attributed Networks (DOMINANT)"""
# Author: Kay Liu <zliu234@uic.edu>
# License: BSD 2 clause

import os
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.utils import to_dense_adj
from torch_geometric.loader import NeighborLoader
from sklearn.utils.validation import check_is_fitted

from . import BaseDetector
from .basic_nn import GCN
from ..utils import validate_device
from ..metrics import eval_roc_auc
import mlflow


class DOMINANT(BaseDetector):
    """
    DOMINANT (Deep Anomaly Detection on Attributed Networks) is an
    anomaly detector consisting of a shared graph convolutional
    encoder, a structure reconstruction decoder, and an attribute
    reconstruction decoder. The reconstruction mean square error of the
    decoders are defined as structure anomaly score and attribute
    anomaly score, respectively.

    See :cite:`ding2019deep` for details.

    Parameters
    ----------
    hid_dim :  int, optional
        Hidden dimension of model. Default: ``0``.
    num_layers : int, optional
        Total number of layers in model. A half (ceil) of the layers
        are for the encoder, the other half (floor) of the layers are
        for decoders. Default: ``4``.
    dropout : float, optional
        Dropout rate. Default: ``0.``.
    weight_decay : float, optional
        Weight decay (L2 penalty). Default: ``0.``.
    act : callable activation function or None, optional
        Activation function if not None.
        Default: ``torch.nn.functional.relu``.
    alpha : float, optional
        Loss balance weight for attribute and structure. ``None`` for
        balancing by standard deviation. Default: ``None``.
    contamination : float, optional
        Valid in (0., 0.5). The proportion of outliers in the data set.
        Used when fitting to define the threshold on the decision
        function. Default: ``0.1``.
    lr : float, optional
        Learning rate. Default: ``0.004``.
    epoch : int, optional
        Maximum number of training epoch. Default: ``5``.
    gpu : int
        GPU Index, -1 for using CPU. Default: ``0``.
    batch_size : int, optional
        Minibatch size, 0 for full batch training. Default: ``0``.
    num_neigh : int, optional
        Number of neighbors in sampling, -1 for all neighbors.
        Default: ``-1``.
    verbose : bool
        Verbosity mode. Turn on to print out log information.
        Default: ``False``.

    Examples
    --------
    >>> from pygod.models import DOMINANT
    >>> model = DOMINANT()
    >>> model.fit(data) # PyG graph data object
    >>> prediction = model.predict(data)
    """

    def __init__(self,
                 hid_dim=64,
                 num_layers=4,
                 dropout=0.3,
                 weight_decay=0.,
                 act=F.relu,
                 alpha=None,
                 contamination=0.1,
                 mlflow_run_id=None,
                 lr=5e-3,
                 epoch=5,
                 gpu=0,
                 batch_size=0,
                 num_neigh=-1,
                 verbose=False):
        super(DOMINANT, self).__init__(contamination=contamination)

        # model param
        self.hid_dim = hid_dim
        self.num_layers = num_layers
        self.dropout = dropout
        self.weight_decay = weight_decay
        self.act = act
        self.alpha = alpha

        # training param
        self.lr = lr
        self.epoch = epoch
        self.device = validate_device(gpu)
        self.batch_size = batch_size
        self.num_neigh = num_neigh

        # other param
        self.verbose = verbose
        self.model = None
        self.mlflow_run_id = mlflow_run_id
        self.last_epoch_loss = -1
        self.first_epoch_loss = -1

    def fit(self, G, y_true=None, eval_G=None):
        """
        Fit detector with input data.

        Parameters
        ----------
        G : torch_geometric.data.Data
            The input data.
        y_true : numpy.ndarray, optional
            The optional outlier ground truth labels used to monitor
            the training progress. They are not used to optimize the
            unsupervised model. Default: ``None``.

        Returns
        -------
        self : object
            Fitted estimator.
        """
        G.node_idx = torch.arange(G.x.shape[0])
        G.s = to_dense_adj(G.edge_index)[0]

        # automated balancing by std
        if self.alpha is None:
            self.alpha = torch.std(G.s).detach() / \
                         (torch.std(G.x).detach() + torch.std(G.s).detach())

        if self.batch_size == 0:
            self.batch_size = G.x.shape[0]
        loader = NeighborLoader(G,
                                [self.num_neigh] * self.num_layers,
                                batch_size=self.batch_size)

        self.model = DOMINANT_Base(in_dim=G.x.shape[1],
                                   hid_dim=self.hid_dim,
                                   num_layers=self.num_layers,
                                   dropout=self.dropout,
                                   act=self.act).to(self.device)

        optimizer = torch.optim.Adam(self.model.parameters(),
                                     lr=self.lr,
                                     weight_decay=self.weight_decay)

        self.model.train()
        decision_scores = np.zeros(G.x.shape[0])
        for epoch in range(self.epoch):
            epoch_loss = 0
            for sampled_data in loader:
                batch_size = sampled_data.batch_size
                node_idx = sampled_data.node_idx
                x, s, edge_index = self.process_graph(sampled_data)

                x_, s_ = self.model(x, edge_index)
                score = self.loss_func(x[:batch_size],
                                       x_[:batch_size],
                                       s[:batch_size, node_idx],
                                       s_[:batch_size])
                decision_scores[node_idx[:batch_size]] = score.detach() \
                    .cpu().numpy()
                loss = torch.mean(score)                
                epoch_loss += loss.item() * batch_size

                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
            
            ## Evaluation loss
            eval_epoch_loss = -1
            if eval_G != None:
                eval_epoch_loss = self.evaluating_function(eval_G)

            if self.verbose:
                print("Epoch {:04d}: Loss {:.4f}"
                      .format(epoch, epoch_loss / G.x.shape[0]), end='')
                with mlflow.start_run(run_id = self.mlflow_run_id, nested=True):
                    mlflow.log_metric(key="epoch_loss", value=epoch_loss/G.x.shape[0], step=epoch)
                    if eval_epoch_loss != 1:
                        mlflow.log_metric(key="eval_epoch_loss", value=eval_epoch_loss, step=epoch)

                if y_true is not None:
                    auc = eval_roc_auc(y_true, decision_scores)
                    print(" | AUC {:.4f}".format(auc), end='')
                    with mlflow.start_run(run_id = self.mlflow_run_id):
                        mlflow.log_metric(key="auc", value= auc, step=epoch)
                print()
            
            if epoch == 0:
                self.first_epoch_loss = epoch_loss/G.x.shape[0]
            self.last_epoch_loss=epoch_loss/G.x.shape[0]
        self.decision_scores_ = decision_scores
        self._process_decision_scores()
        return self

    def evaluating_function(self, eval_G):
        """
        Evaluating function

        Parameters
        ----------
        eval_G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        eval_epoch_loss_avg : float
            The evaluation epoch loss.
        """
        check_is_fitted(self, ['model'])
        eval_G.node_idx = torch.arange(eval_G.x.shape[0])
        eval_G.s = to_dense_adj(eval_G.edge_index)[0]

        loader = NeighborLoader(eval_G,
                                [self.num_neigh] * self.num_layers,
                                batch_size=self.batch_size)

        self.model.eval()
        eval_epoch_loss = 0
        for sampled_data in loader:
            batch_size = sampled_data.batch_size
            node_idx = sampled_data.node_idx

            x, s, edge_index = self.process_graph(sampled_data)

            x_, s_ = self.model(x, edge_index)
            score = self.loss_func(x[:batch_size],
                                   x_[:batch_size],
                                   s[:batch_size, node_idx],
                                   s_[:batch_size])
            
            eval_loss = torch.mean(score)                
            eval_epoch_loss += eval_loss.item() * batch_size

        self.model.train()
        eval_epoch_loss_avg = eval_epoch_loss/eval_G.x.shape[0]
        print('Evaluation Loss: ',eval_epoch_loss_avg)

        return eval_epoch_loss_avg

    def decision_function(self, G):
        """
        Predict raw anomaly score using the fitted detector. Outliers
        are assigned with larger anomaly scores.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        outlier_scores : numpy.ndarray
            The anomaly score of shape :math:`N`.
        """
        check_is_fitted(self, ['model'])
        G.node_idx = torch.arange(G.x.shape[0])
        G.s = to_dense_adj(G.edge_index)[0]

        loader = NeighborLoader(G,
                                [self.num_neigh] * self.num_layers,
                                batch_size=self.batch_size)

        self.model.eval()
        outlier_scores = np.zeros(G.x.shape[0])
        for sampled_data in loader:
            batch_size = sampled_data.batch_size
            node_idx = sampled_data.node_idx

            x, s, edge_index = self.process_graph(sampled_data)

            x_, s_ = self.model(x, edge_index)
            score = self.loss_func(x[:batch_size],
                                   x_[:batch_size],
                                   s[:batch_size, node_idx],
                                   s_[:batch_size])

            outlier_scores[node_idx[:batch_size]] = score.detach() \
                .cpu().numpy()
        return outlier_scores

    def process_graph(self, G):
        """
        Process the raw PyG data object into a tuple of sub data
        objects needed for the model.

        Parameters
        ----------
        G : PyTorch Geometric Data instance (torch_geometric.data.Data)
            The input data.

        Returns
        -------
        x : torch.Tensor
            Attribute (feature) of nodes.
        s : torch.Tensor
            Adjacency matrix of the graph.
        edge_index : torch.Tensor
            Edge list of the graph.
        """
        s = G.s.to(self.device)
        edge_index = G.edge_index.to(self.device)
        x = G.x.to(self.device)

        return x, s, edge_index

    def loss_func(self, x, x_, s, s_):
        # attribute reconstruction loss
        diff_attribute = torch.pow(x - x_, 2)
        attribute_errors = torch.sqrt(torch.sum(diff_attribute, 1))

        # structure reconstruction loss
        diff_structure = torch.pow(s - s_, 2)
        structure_errors = torch.sqrt(torch.sum(diff_structure, 1))

        score = self.alpha * attribute_errors \
                + (1 - self.alpha) * structure_errors
        return score


class DOMINANT_Base(nn.Module):
    def __init__(self,
                 in_dim,
                 hid_dim,
                 num_layers,
                 dropout,
                 act):
        super(DOMINANT_Base, self).__init__()

        # split the number of layers for the encoder and decoders
        decoder_layers = int(num_layers / 2)
        encoder_layers = num_layers - decoder_layers

        self.shared_encoder = GCN(in_channels=in_dim,
                                  hidden_channels=hid_dim,
                                  num_layers=encoder_layers,
                                  out_channels=hid_dim,
                                  dropout=dropout,
                                  act=act)

        self.attr_decoder = GCN(in_channels=hid_dim,
                                hidden_channels=hid_dim,
                                num_layers=decoder_layers,
                                out_channels=in_dim,
                                dropout=dropout,
                                act=act)

        self.struct_decoder = GCN(in_channels=hid_dim,
                                  hidden_channels=hid_dim,
                                  num_layers=decoder_layers - 1,
                                  out_channels=in_dim,
                                  dropout=dropout,
                                  act=act)

    def forward(self, x, edge_index):
        # encode
        h = self.shared_encoder(x, edge_index)
        # decode feature matrix
        x_ = self.attr_decoder(h, edge_index)
        # decode adjacency matrix
        h_ = self.struct_decoder(h, edge_index)
        s_ = h_ @ h_.T
        # return reconstructed matrices
        return x_, s_

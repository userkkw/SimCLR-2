import os

from data.plasmodium_dataset import PlasmodiumDataset

os.environ["CUDA_VISIBLE_DEVICES"] = "2"

import torch
import torchvision
import argparse
import numpy as np

from data.matek_dataset import MatekDataset
from data.jurkat_dataset import JurkatDataset
from experiment import ex
from model import load_model
from utils import post_config_hook

from modules import LogisticRegression
from modules.transformations import TransformsSimCLR
from sklearn.metrics import classification_report


def inference(loader, context_model, device):
    feature_vector = []
    labels_vector = []
    for step, (x, y) in enumerate(loader):
        x = x.to(device)

        # get encoding
        with torch.no_grad():
            h, z = context_model(x)

        h = h.detach()

        feature_vector.extend(h.cpu().detach().numpy())
        labels_vector.extend(y.numpy())

        if step % 20 == 0:
            print(f"Step [{step}/{len(loader)}]\t Computing features...")

    feature_vector = np.array(feature_vector)
    labels_vector = np.array(labels_vector)
    print("Features shape {}".format(feature_vector.shape))
    return feature_vector, labels_vector


def get_features(context_model, train_loader, test_loader, device):
    train_X, train_y = inference(train_loader, context_model, device)
    test_X, test_y = inference(test_loader, context_model, device)
    return train_X, train_y, test_X, test_y


def create_data_loaders_from_arrays(X_train, y_train, X_test, y_test, batch_size):
    train = torch.utils.data.TensorDataset(
        torch.from_numpy(X_train), torch.from_numpy(y_train)
    )
    train_loader = torch.utils.data.DataLoader(
        train, batch_size=batch_size, shuffle=False
    )

    test = torch.utils.data.TensorDataset(
        torch.from_numpy(X_test), torch.from_numpy(y_test)
    )
    test_loader = torch.utils.data.DataLoader(
        test, batch_size=batch_size, shuffle=False
    )
    return train_loader, test_loader


def train(args, loader, simclr_model, model, criterion, optimizer):
    loss_epoch = 0
    accuracy_epoch = 0
    for step, (x, y) in enumerate(loader):
        optimizer.zero_grad()

        x = x.to(args.device)
        y = y.to(args.device)

        output = model(x)
        loss = criterion(output, y)

        predicted = output.argmax(1)
        acc = (predicted == y).sum().item() / y.size(0)
        accuracy_epoch += acc

        loss.backward()
        optimizer.step()

        loss_epoch += loss.item()
        # if step % 100 == 0:
        #     print(
        #         f"Step [{step}/{len(loader)}]\t Loss: {loss.item()}\t Accuracy: {acc}"
        #     )

    return loss_epoch, accuracy_epoch


def test(args, loader, simclr_model, model, criterion, optimizer):
    loss_epoch = 0
    accuracy_epoch = 0
    model.eval()
    gt = []
    pd = []

    if args.dataset == "MATEK":
        target_names = ['BAS', 'EBO', 'EOS', 'KSC', 'LYA', 'LYT', 'MMZ', 'MOB',
                        'MON', 'MYB', 'MYO', 'NGB', 'NGS', 'PMB', 'PMO']
    elif args.dataset == "JURKAT":
        target_names = ['Anaphase', 'G1', 'G2', 'Metaphase', 'Prophase', 'S', 'Telophase']
    else:
        target_names = ["parasitized", "uninfected"]

    for step, (x, y) in enumerate(loader):
        model.zero_grad()

        x = x.to(args.device)
        y = y.to(args.device)

        output = model(x)
        loss = criterion(output, y)

        predicted = output.argmax(1)
        acc = (predicted == y).sum().item() / y.size(0)
        accuracy_epoch += acc

        loss_epoch += loss.item()

        gt.extend(y.tolist())
        pd.extend(predicted.tolist())

    report = classification_report(gt, pd, target_names=target_names, zero_division=1)

    return loss_epoch, accuracy_epoch, report


@ex.automain
def main(_run, _log):
    args = argparse.Namespace(**_run.config)
    args = post_config_hook(args, _run)

    args.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    root = "./datasets"
    train_sampler = None
    valid_sampler = None

    if args.dataset == "STL10":
        dataset = torchvision.datasets.STL10(
            root,
            split="train",
            download=True,
            transform=TransformsSimCLR(size=224).test_transform,
        )
        test_dataset = torchvision.datasets.STL10(
            root,
            split="test",
            download=True,
            transform=TransformsSimCLR(size=224).test_transform,
        )
    elif args.dataset == "CIFAR10":
        dataset = torchvision.datasets.CIFAR10(
            root,
            train=True,
            download=True,
            transform=TransformsSimCLR(size=224).test_transform,
        )
        test_dataset = torchvision.datasets.CIFAR10(
            root,
            train=False,
            download=True,
            transform=TransformsSimCLR(size=224).test_transform,
        )
    elif args.dataset == "MATEK":
        dataset, train_sampler, valid_sampler = MatekDataset(
            root=root, transforms=TransformsSimCLR(size=128).test_transform, test_size=args.test_size
        ).get_dataset()
    elif args.dataset == "JURKAT":
        dataset, train_sampler, valid_sampler = JurkatDataset(
            root=root, transforms=TransformsSimCLR(size=64).test_transform, test_size=args.test_size
        ).get_dataset()
    elif args.dataset == "PLASMODIUM":
        dataset, train_sampler, valid_sampler = PlasmodiumDataset(
            root=root, transforms=TransformsSimCLR(size=128).test_transform, test_size=args.test_size
        ).get_dataset()
    else:
        raise NotImplementedError

    train_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.logistic_batch_size,
        shuffle=(train_sampler is None),
        drop_last=True,
        num_workers=args.workers,
        sampler=train_sampler,
    )

    test_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.logistic_batch_size,
        shuffle=(valid_sampler is None),
        drop_last=True,
        num_workers=args.workers,
        sampler=valid_sampler,
    )

    simclr_model, _, _ = load_model(args, train_loader, reload_model=True)
    simclr_model = simclr_model.to(args.device)
    simclr_model.eval()

    # Logistic Regression
    n_classes = args.n_classes  # stl-10
    model = LogisticRegression(simclr_model.n_features, n_classes)
    model = model.to(args.device)

    optimizer = torch.optim.Adam(model.parameters(), lr=3e-4)
    criterion = torch.nn.CrossEntropyLoss()

    print("### Creating features from pre-trained context model ###")
    (train_X, train_y, test_X, test_y) = get_features(
        simclr_model, train_loader, test_loader, args.device
    )

    arr_train_loader, arr_test_loader = create_data_loaders_from_arrays(
        train_X, train_y, test_X, test_y, args.logistic_batch_size
    )

    for epoch in range(args.logistic_epochs):
        loss_epoch, accuracy_epoch = train(
            args, arr_train_loader, simclr_model, model, criterion, optimizer
        )
        print(
            f"Epoch [{epoch}/{args.logistic_epochs}]\t Loss: {loss_epoch / len(train_loader)}\t Accuracy: {accuracy_epoch / len(train_loader)}"
        )

    # final testing
    loss_epoch, accuracy_epoch, report = test(
        args, arr_test_loader, simclr_model, model, criterion, optimizer
    )
    print(
        f"[FINAL]\t Loss: {loss_epoch / len(test_loader)}\t Accuracy: {accuracy_epoch / len(test_loader)}"
    )

    print(report)

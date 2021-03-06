# coding=utf-8
import argparse
import json
import logging
import os
import sys
import copy
import time
from PIL import Image

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
import torch.utils.data
import torch.utils.data.distributed
from torchvision import models, datasets, transforms

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler(sys.stdout))

Image.MAX_IMAGE_PIXELS = None
NUM_CLASS = 4

"""
Method to augment and load data on CPU with PyTorch Dataloaders
"""


def augmentation_pytorch(train_dir, batch_size, workers, is_distributed, use_cuda):
    aug_ops = [
        transforms.RandomHorizontalFlip(),
        transforms.RandomVerticalFlip(),
        transforms.RandomRotation(5)
    ]
    
    crop_norm_ops = [
        transforms.RandomResizedCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406],
                             [0.229, 0.224, 0.225])
    ]

    train_aug_ops = []
    train_aug_ops = train_aug_ops + aug_ops

    data_transforms = {
        'train': transforms.Compose(train_aug_ops + crop_norm_ops),
        'val': transforms.Compose(crop_norm_ops),
    }

    image_datasets = {x: datasets.ImageFolder(os.path.join(train_dir, x),
                                              data_transforms[x])
                      for x in ['train', 'val']}
    train_sampler = torch.utils.data.distributed.DistributedSampler(image_datasets) if is_distributed else None
    dataloaders = {x: torch.utils.data.DataLoader(dataset=image_datasets[x],
                                                  batch_size=batch_size,
                                                  shuffle=train_sampler,
                                                  num_workers=workers,
                                                  pin_memory=True if use_cuda else False)
                   for x in ['train', 'val']}

    dataset_sizes = {x: len(image_datasets[x]) for x in ['train', 'val']}
    return dataloaders, dataset_sizes

"""
--------------------------------
Method to save the trained model
--------------------------------
"""

def save_model(model_dir, model_ft):
    logger.info("Saving the model.")
    path = os.path.join(model_dir, 'model.pth')
    torch.save(model_ft.cpu().state_dict(), path)
    
    
"""
Method to train models for number of epochs
"""


def run_training_epochs(model_ft, num_epochs, criterion, optimizer_ft, dataloaders, dataset_sizes, device):
    best_model_wts = copy.deepcopy(model_ft.state_dict())
    best_acc = 0.0

    total_epoch_time = 0
    for epoch in range(num_epochs):
        print('Running Epoch {}/{}'.format(epoch + 1, num_epochs))

        epoch_start_time = time.time()

        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:

            if phase == 'train':
                model_ft.train()
            else:
                model_ft.eval()

            running_loss = 0.0
            running_corrects = 0
            
            for inputs, labels in dataloaders[phase]:
                inputs = inputs.to(device)
                labels = labels.to(device)

                optimizer_ft.zero_grad()
                with torch.set_grad_enabled(phase == 'train'):
                    outputs = model_ft(inputs)
                    _, preds = torch.max(outputs, 1)
                    loss = criterion(outputs, labels)
                    if phase == 'train':
                        loss.backward()
                        optimizer_ft.step()
                running_loss += loss.item() * inputs.size(0)
                running_corrects += torch.sum(preds == labels.data)

            epoch_loss = running_loss / dataset_sizes[phase]
            epoch_acc = running_corrects / dataset_sizes[phase]
            print('{}-loss: {:.4f} {}-acc: {:.4f}'.format(
                phase, epoch_loss, phase, epoch_acc))

            if phase == 'val' and epoch_acc > best_acc:
                best_model_wts = copy.deepcopy(model_ft.state_dict())

        epoch_time_elapsed = time.time() - epoch_start_time
        print('Epoch completed in {:.2f}s'.format(epoch_time_elapsed))
        total_epoch_time = total_epoch_time + epoch_time_elapsed

    # Calculating Seconds/ Epoch: Metric used for comparing performance for the experiemnts
    print('-' * 25)
    print('Seconds per Epoch: {:.2f}'.format(total_epoch_time / num_epochs))

    model_ft.load_state_dict(best_model_wts)
    return model_ft, best_acc


def training(args):
    num_gpus = args.num_gpus
    hosts = args.hosts
    current_host = args.current_host
    backend = args.backend
    seed = args.seed    
    model_dir = args.model_dir

    is_distributed = len(hosts) > 1 and backend is not None
    logger.debug("Distributed training - {}".format(is_distributed))
    use_cuda = num_gpus > 0
    logger.debug("Number of gpus available - {}".format(num_gpus))
    device = torch.device("cuda" if use_cuda else "cpu")

    world_size = len(hosts)
    os.environ['WORLD_SIZE'] = str(world_size)
    host_rank = hosts.index(current_host)

    if is_distributed:
        # Initialize the distributed environment.
        dist.init_process_group(backend=backend, rank=host_rank, world_size=world_size)
        logger.info('Initialized the distributed environment: \'{}\' backend on {} nodes. '.format(
            backend, dist.get_world_size()) + 'Current host rank is {}. Number of gpus: {}'.format(
            dist.get_rank(), num_gpus))
    # set the seed for generating random numbers
    torch.manual_seed(seed)

    if use_cuda:
        torch.cuda.manual_seed(seed)

    # Loading training and validation data
    batch_size = args.batch_size
    train_dir = args.train_dir

    # Set to the available #CPUs here ??? Hits the file system concurrency with large #workers for large #CPU instances
    workers = os.cpu_count() if use_cuda else 0
    dataloaders, dataset_sizes = augmentation_pytorch(train_dir, batch_size, workers, is_distributed, use_cuda)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

    # Deciding on the model to use
    model_ft = models.resnet18(pretrained=False)
    model_ft = model_ft.to(device)

    if is_distributed and use_cuda:
        model_ft = torch.nn.parallel.DistributedDataParallel(model_ft)
    else:
        model_ft = torch.nn.DataParallel(model_ft)

    num_epochs = args.epochs
    criterion = nn.CrossEntropyLoss()
    optimizer_ft = optim.SGD(model_ft.parameters(), args.lr, args.momentum)

    # Running Model Training   
    since = time.time()

    # Not using the trained model or accuracy score for this experiment
    model_ft, best_acc = run_training_epochs(model_ft,
                                             num_epochs,
                                             criterion,
                                             optimizer_ft,
                                             dataloaders,
                                             dataset_sizes,
                                             device)
    time_elapsed = time.time() - since
    
    # Saving model
    save_model(model_dir, model_ft)


def input_fn(request_body, request_content_type):
    
    data_transform=transforms.Compose([transforms.RandomResizedCrop(224),
                                       transforms.ToTensor(),
                                       transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ])

    if request_content_type == 'application/x-image': 
        image_as_bytes = io.BytesIO(request_body)
        image = Image.open(image_as_bytes)
        image_transformed = data_transform(image)
        image_tensor = image_transformed.unsqueeze(dim=0)
    else:
        print("not support this type yet")
        raise ValueError("not support this type yet")
        
    return image_tensor


def model_fn(model_dir):
    
    model_ft = models.resnet18(pretrained=False)
    for param in model_ft.features.parameters():
            param.require_grad = False
    num_ftrs = model_ft.classifier[6].in_features
    features = list(model_ft.classifier.children())[:-1]
    features.extend([nn.Linear(num_ftrs, NUM_CLASS)]) 
    model_ft.classifier = nn.Sequential(*features)
    
    model_ft = nn.DataParallel(model_ft)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model_ft = model_ft.to(device) 
    
    with open(os.path.join(model_dir, 'model.pth'), 'rb') as f:
        model_ft.load_state_dict(torch.load(f))
    return model_ft


def predict_fn(input_data, model):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model.to(device)
    model.eval()
    output = model(input_data)
    pred = output.max(1, keepdim=True)[1]
    return pred

def output_fn(prediction, content_type):
     return json.dumps(prediction)

    

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--batch-size', type=int, default=32, metavar='N',
                        help='input batch size for training (default: 32)')
    parser.add_argument('--epochs', type=int, default=2, metavar='N',
                        help='number of epochs to train (default: 2)')
    parser.add_argument('--lr', type=float, default=0.001, metavar='LR',
                        help='learning rate (default: 0.001)')
    parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                        help='SGD momentum (default: 0.5)')
    parser.add_argument('--seed', type=int, default=42, metavar='S',
                        help='random seed (default: 42)')
    parser.add_argument('--backend', type=str, default='nccl',
                        help='backend for distributed training (tcp, gloo on cpu and gloo, nccl on gpu)')

    parser.add_argument('--hosts', type=list, default=json.loads(os.environ['SM_HOSTS']))
    parser.add_argument('--current-host', type=str, default=os.environ['SM_CURRENT_HOST'])
    parser.add_argument('--model-dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--num-gpus', type=int, default=os.environ['SM_NUM_GPUS'])
    parser.add_argument('--train_dir', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--val_dir', type=str, default=os.environ['SM_CHANNEL_VAL'])

    training(parser.parse_args())
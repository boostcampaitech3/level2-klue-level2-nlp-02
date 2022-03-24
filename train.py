import os
import torch
import random
import warnings
import sklearn
import numpy as np
import pandas as pd
from tqdm import tqdm
import pickle as pickle
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score
from torch.utils.data import DataLoader
from torch.optim import AdamW
from transformers import AutoTokenizer, AutoConfig, AutoModelForSequenceClassification, TrainingArguments, Trainer
from load_data import *

import argparse
import wandb

warnings.filterwarnings("ignore")

def klue_re_micro_f1(preds, labels):
    """KLUE-RE micro f1 (except no_relation)"""
    label_list = ['no_relation', 'org:top_members/employees', 'org:members',
       'org:product', 'per:title', 'org:alternate_names',
       'per:employee_of', 'org:place_of_headquarters', 'per:product',
       'org:number_of_employees/members', 'per:children',
       'per:place_of_residence', 'per:alternate_names',
       'per:other_family', 'per:colleagues', 'per:origin', 'per:siblings',
       'per:spouse', 'org:founded', 'org:political/religious_affiliation',
       'org:member_of', 'per:parents', 'org:dissolved',
       'per:schools_attended', 'per:date_of_death', 'per:date_of_birth',
       'per:place_of_birth', 'per:place_of_death', 'org:founded_by',
       'per:religion']
    no_relation_label_idx = label_list.index("no_relation")
    label_indices = list(range(len(label_list)))
    label_indices.remove(no_relation_label_idx)
    return sklearn.metrics.f1_score(labels, preds, average="micro", labels=label_indices) * 100.0

def klue_re_auprc(probs, labels):
    """KLUE-RE AUPRC (with no_relation)"""
    labels = np.eye(30)[labels]

    score = np.zeros((30,))
    for c in range(30):
        # print("labels: ", labels)
        targets_c = labels.take([c], axis=1).ravel()
        # print("targets_c: ", targets_c)
        preds_c = probs.take([c], axis=1).ravel()
        # print("preds_c: ", preds_c)
        precision, recall, _ = sklearn.metrics.precision_recall_curve(targets_c, preds_c)
        # print("precision, recall: ", precision, recall)
        score[c] = sklearn.metrics.auc(recall, precision)
        # print("score: ", score)
    return np.average(score) * 100.0


def compute_metrics(pred, labels):
    """ validation을 위한 metrics function """
    # labels = pred.label_ids
    # preds = pred.predictions.argmax(-1)
    # probs = pred.predictions
    pred = pred.detach().cpu().numpy()
    labels = labels.detach().cpu().numpy()
    preds = pred.argmax(-1)
    probs = pred

    # calculate accuracy using sklearn's function
    f1 = klue_re_micro_f1(preds, labels)
    auprc = klue_re_auprc(probs, labels)
    acc = accuracy_score(labels, preds) # 리더보드 평가에는 포함되지 않습니다.

    return {
        'micro f1 score': f1,
        'auprc' : auprc,
        'accuracy': acc,
    }

def label_to_num(label):
    num_label = []
    with open('dict_label_to_num.pkl', 'rb') as f:
        dict_label_to_num = pickle.load(f)
    for v in label:
        num_label.append(dict_label_to_num[v])
    
    return num_label

def seed_everything(seed=42):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

def train(args):
    # load model and tokenizer
    MODEL_NAME = args.model
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    # load dataset
    dataset = load_data("../dataset/train/train.csv")
        
    train_dataset, valid_dataset = train_test_split(dataset, test_size=args.val_ratio, shuffle=True, stratify=dataset['label'], random_state=args.seed)

    train_label = label_to_num(train_dataset['label'].values)
    valid_label = label_to_num(valid_dataset['label'].values)

    # tokenizing dataset
    tokenized_train = tokenized_dataset(train_dataset, tokenizer)
    tokenized_valid = tokenized_dataset(valid_dataset, tokenizer)

    # make dataset for pytorch.
    RE_train_dataset = RE_Dataset(tokenized_train, train_label)
    RE_valid_dataset = RE_Dataset(tokenized_valid, valid_label)

    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    print(device)
    # setting model hyperparameter
    model_config = AutoConfig.from_pretrained(MODEL_NAME)
    model_config.num_labels = 30

    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, config=model_config)
    print(model.config)
    model.to(device)
    
    train_loader = DataLoader(RE_train_dataset, batch_size=args.batch_size, shuffle=True)
    valid_loader = DataLoader(RE_valid_dataset, batch_size=args.valid_batch_size, shuffle=True)

    optim = AdamW(model.parameters(), lr=args.lr)

    wandb.log({
        "model":args.model,
        "seed":args.seed,
        "epochs":args.epochs,
        "batch_size":args.batch_size,
        "optimizer":args.optimizer,
        "lr":args.lr,
        "val_ratio":args.val_ratio,
    })

    for epoch in range(args.epochs):
        total_loss, total_idx = 0, 0
        eval_total_loss, eval_total_idx = 0, 0
        total_f1, total_auprc, total_acc = 0, 0, 0
        eval_total_f1, eval_total_auprc, eval_total_acc = 0, 0, 0

        model.train()
        
        for idx, batch in enumerate(tqdm(train_loader)):
            total_idx += 1

            optim.zero_grad()
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = batch['labels'].to(device)
            outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
            pred = outputs[1]
            metric = compute_metrics(pred, labels)

            loss = outputs[0]

            total_loss += loss
            total_f1 += metric['micro f1 score']
            # total_auprc += metric['auprc']
            total_acc += metric['accuracy']

            average_loss = total_loss/total_idx
            average_f1 = total_f1/total_idx
            average_acc = total_acc/total_idx

            wandb.log({
                "epoch":epoch,
                "train_loss":average_loss,
                "train_f1":average_f1,
                "train_acc":average_acc
                })

            if idx%args.logging_step == 0:
                print(f"[TRAIN][EPOCH:({epoch + 1}/{args.epochs}) | loss:{average_loss:4.2f} | ", end="")
                print(f"micro_f1_score:{average_f1:4.2f} | accuracy:{average_acc:4.2f}]")

            loss.backward()
            optim.step()
        print("--------------------------------------------------------------------------")
        print(f"[EVALUATION] EPOCH:({epoch + 1}/{args.epochs})")
        with torch.no_grad():
            model.eval()        
            for batch in tqdm(valid_loader):
                eval_total_idx += 1

                input_ids = batch['input_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                labels = batch['labels'].to(device)
                outputs = model(input_ids, attention_mask=attention_mask, labels=labels)
                pred = outputs[1]
                eval_metric = compute_metrics(pred, labels)

                loss = outputs[0]

                eval_total_loss += loss
                eval_total_f1 += eval_metric['micro f1 score']
                eval_total_auprc += eval_metric['auprc']
                eval_total_acc += eval_metric['accuracy']

                eval_average_loss = eval_total_loss/eval_total_idx
                eval_average_f1 = eval_total_f1/eval_total_idx
                eval_average_acc = eval_total_acc/eval_total_idx

                
                wandb.log({
                    "eval_loss":eval_average_loss,
                    "eval_f1":eval_average_f1,
                    "eval_acc":eval_average_acc
                    })

            print(f"[EVAL][loss:{eval_average_loss:4.2f} | auprc:{eval_total_auprc/eval_total_idx:4.2f} | ", end="")
            print(f"micro_f1_score:{eval_average_f1:4.2f} | accuracy:{eval_average_acc:4.2f}]")

        print("--------------------------------------------------------------------------")

    best_save_path = args.best_save_dir
    model.save_pretrained(best_save_path)
    tokenizer.save_pretrained(best_save_path)
    wandb.finish()
    
def main(args):
    seed_everything(args.seed)
    wandb.init(project=args.project_name, entity="salt-bread", name=args.report_name)
    train(args)

if __name__ == '__main__':
    # wandb.login()

    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default="klue/bert-base")
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--logging_step', type=int, default=100)
    parser.add_argument('--batch_size', type=int, default=16)
    parser.add_argument('--valid_batch_size', type=int, default=64)
    parser.add_argument('--optimizer', type=str, default="AdamW")
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--val_ratio', type=float, default=0.1)
    parser.add_argument('--criterion', type=str, default=None)
    parser.add_argument('--save_dir', type=str, default="./results")
    parser.add_argument('--best_save_dir', type=str, default="./best_model")
    parser.add_argument('--report_name', type=str)
    parser.add_argument('--project_name', type=str, default="salt_v1")

    args = parser.parse_args()
    main(args)

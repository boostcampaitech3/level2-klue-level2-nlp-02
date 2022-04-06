import json
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
from functools import partial
from sklearn.metrics import accuracy_score, recall_score, precision_score, f1_score, confusion_matrix, classification_report
from sklearn.model_selection import StratifiedKFold
from torch.utils.data import DataLoader
from torch.optim import AdamW
from adamp import AdamP
from torch.optim.lr_scheduler import StepLR, ReduceLROnPlateau
from collate_fn import collate_fn
from loss import create_criterion
from transformers import AutoTokenizer, AutoConfig, AutoModelForSequenceClassification, TrainingArguments, Trainer, AutoModel
from scheduler import create_lr_scheduler
from load_data import *

import argparse
import wandb

os.environ["TOKENIZERS_PARALLELISM"] = "true"
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

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

def str_cf(cf):
    idx = 0
    result = "     "
    for i in range(30):
        result += ("%4d" %i)
    result += '\n    '
    result += ('----'*30 + '\n')
    for i in cf:
        result += ("%-3d| " % idx)
        idx += 1
        for j in i:
            result += ("%4d" % j)
        result += '\n'
    return result


def train(args):
    # get random number to choose example sentence
    data_idx = random.randint(0, 100)
    # hold seeds
    seed_everything(args.seed)
    # load model and tokenizer
    MODEL_NAME = args.model
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    special_tokens={
        'origin':[],
        'entity':["[ENT]", "[/ENT]"],
        'type_entity':["[ORG]","[/ORG]","[PER]","[/PER]","[POH]","[/POH]","[LOC]","[/LOC]","[DAT]","[/DAT]","[NOH]","[/NOH]"],
        'sub_obj':['[SUB_ENT]','[/SUB_ENT]','[OBJ_ENT]','[/OBJ_ENT]']
    }
    num_added_token = tokenizer.add_special_tokens({"additional_special_tokens":special_tokens.get(args.token_type, [])})    

    skf = StratifiedKFold(n_splits=args.kfold_splits, random_state = args.seed, shuffle=True)

    # load dataset
    dataset = load_data("../dataset/train/train.csv", token_type=args.token_type)
    dataset_label = label_to_num(dataset['label'].values)

    # best_eval_loss = 1e9
    # best_eval_f1 = 0
    # total_idx = 0
    for kfold_idx, (train_idx, valid_idx) in enumerate(skf.split(dataset, dataset_label)):
        print(f'########  kfold : {kfold_idx} start  ########')

        train_dataset, valid_dataset = dataset.iloc[train_idx,:], dataset.iloc[valid_idx,:]

        train_label = label_to_num(train_dataset['label'].values)
        valid_label = label_to_num(valid_dataset['label'].values)

        # tokenizing dataset
        tokenized_train = tokenized_dataset(train_dataset, tokenizer,sep_type=args.sep_type)
        tokenized_valid = tokenized_dataset(valid_dataset, tokenizer,sep_type=args.sep_type)

        # make dataset for pytorch.
        RE_train_dataset = RE_Dataset(tokenized_train, train_label)
        RE_valid_dataset = RE_Dataset(tokenized_valid, valid_label)
        print("[dataset 예시]", tokenizer.decode(RE_train_dataset[data_idx]['input_ids']), sep='\n')

        device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        print(device)
        # setting model hyperparameter
        model_config = AutoConfig.from_pretrained(MODEL_NAME)
        model_config.num_labels = 30
        model_config.hidden_dropout_prob = args.dropout
        model_config.attention_probs_dropout_prob = args.dropout

        model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, config=model_config)
        model.resize_token_embeddings(num_added_token + tokenizer.vocab_size)
        print(model.config)
        model.to(device)
        ###############################################
        # b_model = AutoModel.from_pretrained(MODEL_NAME, config= model_config)
        # b_model.resize_token_embeddings(num_added_token + tokenizer.vocab_size)
        # model = TunedModelLSTM(b_model, 30, device, args.dropout, lstm_layers = args.lstm_layers)

        # #print(model.config)
        # model.to(device)
        # ################################################

        
        train_loader = DataLoader(RE_train_dataset, batch_size=args.batch_size, shuffle=True, drop_last = True, collate_fn=partial(collate_fn, sep_token_id=tokenizer.sep_token_id))
        valid_loader = DataLoader(RE_valid_dataset, batch_size=args.valid_batch_size, shuffle=True, drop_last = False, collate_fn=partial(collate_fn, sep_token_id=tokenizer.sep_token_id))

        if args.optimizer == 'AdamW':
            optim = AdamW(model.parameters(), lr=args.lr)
        elif args.optimizer == 'AdamP': # lr=0.001
            optim = AdamP(model.parameters(), lr=args.lr, betas=(0.9, 0.999), weight_decay=1e-2)
        else:
            optim = AdamW(model.parameters(), lr=args.lr)
        # optim = AdamW([
        #                 {'params' : model.base_model.parameters(), 'lr':args.lr},
        #                 {'params':model.linear.parameters(), 'lr':args.lr},
        #                 {'params' : model.rnn.parameters(), 'lr' : 0.001},
        #                 {'params' : model.rnn_lin.parameters(), 'lr' : 0.001},
        #                 ])

        for param_group in optim.param_groups:
            print(param_group['lr'])


        criterion = create_criterion(args.criterion)

        if args.lr_scheduler:
            # lr_scheduler = create_lr_scheduler(args.lr_scheduler)
            # scheduler = lr_scheduler(optim)
            
        #     scheduler = StepLR(optim, 20, gamma=0.5)
            if args.lr_scheduler == 'reduceLR':
                scheduler = ReduceLROnPlateau(optim, mode='min', factor=0.5, patience=0, verbose=1)
            elif args.lr_scheduler == 'StepLR':
                scheduler = StepLR(optim, 20, gamma=0.5)

        save_path = args.save_dir
        k_save_path = os.path.join(save_path, str(kfold_idx))
        os.makedirs(k_save_path, exist_ok=True)

        model_config_parameters = {
            "model":args.model,
            "seed":args.seed,
            "epochs":args.epochs,
            "batch_size":args.batch_size,
            "token_type":args.token_type,
            "optimizer":args.optimizer,
            "lr":args.lr,
            "val_ratio":args.val_ratio,
            "sep_type":args.sep_type,
            "kfold_splits":args.kfold_splits,
        }

        if not os.path.exists(save_path):
            os.mkdir(save_path)

        os.makedirs(save_path, exist_ok=True)
        with open(os.path.join(save_path, "model_config_parameters.json"), 'w') as f:
            json.dump(model_config_parameters, f, indent=4)
        print(f"{save_path}에 model_config_parameter.json 파일 저장")

        f = open(os.path.join(k_save_path, "report.txt"), 'w')
        f.close()

        tokenizer.save_pretrained(save_path)
        print(f"{save_path}에 tokenizer 저장")

        if args.wandb == "True":
            name = args.report_name +'_'+ str(kfold_idx)
            wandb.init(project=args.project_name, entity="salt-bread", name=name, config=model_config_parameters)

        best_eval_loss = 1e9
        best_eval_f1 = 0
        total_idx = 0
        
        for epoch in range(args.epochs):
            total_f1, total_loss, total_acc = 0, 0, 0
            average_loss, average_f1, average_acc = 0,0,0
            #model.train()
            
            for idx, batch in enumerate(tqdm(train_loader)):
                model.train()
                total_idx += 1

                optim.zero_grad()
                input_ids = batch['input_ids'].to(device)
                attention_mask = batch['attention_mask'].to(device)
                token_type_ids =  batch['token_type_ids'].to(device)
                labels = batch['labels'].to(device)
                outputs = model(input_ids, attention_mask=attention_mask, labels=labels, token_type_ids=token_type_ids)
                pred = outputs[1]
                metric = compute_metrics(pred, labels)

                # loss = outputs[0]
                loss = criterion(pred, labels)

                loss.backward()
                optim.step()
                total_loss += loss
                total_f1 += metric['micro f1 score']
                # total_auprc += metric['auprc']
                total_acc += metric['accuracy']

                average_loss = total_loss/(idx+1)
                average_f1 = total_f1/(idx+1)
                average_acc = total_acc/(idx+1)

                if idx%args.logging_step == 0:
                    print(f"[K_FOLD:({kfold_idx})][TRAIN][EPOCH:({epoch + 1}/{args.epochs}) | loss:{average_loss:4.2f} | ", end="")
                    print(f"micro_f1_score:{average_f1:4.2f} | accuracy:{average_acc:4.2f}]")

            
                if total_idx%args.eval_step == 0:
                    eval_total_loss, eval_total_f1, eval_total_auprc, eval_total_acc = 0, 0, 0, 0
                    label_list, pred_list = [], []
                    with torch.no_grad():
                        model.eval()
                        print("--------------------------------------------------------------------------")
                        print(f"[K_FOLD:({kfold_idx})][EVAL] STEP:{total_idx}, BATCH SIZE:{args.batch_size}")
                        for idx, batch in enumerate(tqdm(valid_loader)):

                            input_ids = batch['input_ids'].to(device)
                            attention_mask = batch['attention_mask'].to(device)
                            token_type_ids =  batch['token_type_ids'].to(device)
                            labels = batch['labels'].to(device)
                            outputs = model(input_ids, attention_mask=attention_mask, labels=labels, token_type_ids=token_type_ids)
                            pred = outputs[1]
                            eval_metric = compute_metrics(pred, labels)

                            # loss = outputs[0]
                            loss = criterion(pred, labels)

                            eval_total_loss += loss
                            eval_total_f1 += eval_metric['micro f1 score']
                            eval_total_auprc += eval_metric['auprc']
                            eval_total_acc += eval_metric['accuracy']

                            pred_list.extend(list(pred.detach().cpu().numpy().argmax(-1)))
                            label_list.extend(list(labels.detach().cpu().numpy()))

                        eval_average_loss = eval_total_loss / len(valid_loader)
                        eval_average_f1 = eval_total_f1 / len(valid_loader)
                        eval_total_auprc = eval_total_auprc / len(valid_loader)
                        eval_average_acc = eval_total_acc / len(valid_loader)

                        cf = confusion_matrix(label_list, pred_list)
                        cf = str_cf(cf)
                        cr = classification_report(label_list, pred_list)
                        with open(os.path.join(k_save_path, "report.txt"), "a") as f:
                            f.write("#"*10 + f"  {total_idx}  " + "#"*100 + '\n')
                            f.write(cf)
                            f.write(cr)
                            f.write('\n')

                        if args.checkpoint:
                            model.save_pretrained(os.path.join(k_save_path, f"checkpoint-{total_idx}"))
                            # os.makedirs( os.path.join(k_save_path, f"checkpoint-{total_idx}") , exist_ok=True)
                            # torch.save(model, os.path.join(k_save_path, f"checkpoint-{total_idx}", "model.bin"))

                        if eval_average_loss < best_eval_loss:
                            model.save_pretrained(os.path.join(k_save_path, "best_loss"))
                            # os.makedirs( os.path.join(k_save_path, "best_loss") , exist_ok=True)
                            # torch.save(model, os.path.join(k_save_path, "best_loss", "model.bin"))
                            best_eval_loss = eval_average_loss

                        if eval_average_f1 > best_eval_f1:
                            model.save_pretrained(os.path.join(k_save_path, "best_f1"))
                            # os.makedirs( os.path.join(k_save_path, "best_f1") , exist_ok=True)
                            # torch.save(model, os.path.join(k_save_path, "best_f1", "model.bin"))
                            best_eval_f1 = eval_average_f1

                        if args.wandb == "True":
                            wandb.log({
                                "step":total_idx,
                                "eval_loss":eval_average_loss,
                                "eval_f1":eval_average_f1,
                                "eval_acc":eval_average_acc
                                })

                        

                        print(f"[K_FOLD:({kfold_idx})][EVAL][loss:{eval_average_loss:4.2f} | auprc:{eval_total_auprc:4.2f} | ", end="")
                        print(f"micro_f1_score:{eval_average_f1:4.2f} | accuracy:{eval_average_acc:4.2f}]")

                    print("--------------------------------------------------------------------------")
            if args.wandb == "True":
                wandb.log({
                    "epoch":epoch+1,
                    "train_loss":average_loss,
                    "train_f1":average_f1,
                    "train_acc":average_acc,
                    "learning_rate": optim.param_groups[0]['lr']
                    })
            
            # if args.lr_scheduler:
            #     scheduler.step(eval_average_loss)
            if args.lr_scheduler == 'reduceLR':
                scheduler.step(eval_average_loss)
            elif args.lr_scheduler == 'StepLR':
                scheduler.step()

        
        if args.wandb == "True":
            wandb.finish()


def main(args):
    train(args)

if __name__ == '__main__':
    # wandb.login()

    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default="klue/roberta-large")
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--epochs', type=int, default=5)
    parser.add_argument('--logging_step', type=int, default=100)
    parser.add_argument('--eval_step', type=int, default=100)
    parser.add_argument('--checkpoint', type=bool, default=False)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--valid_batch_size', type=int, default=64)
    parser.add_argument('--optimizer', type=str, default="AdamW") # 'AdamW', 'AdamP'
    parser.add_argument('--lr', type=float, default=5e-5)
    parser.add_argument('--c_lr', type=float, default=1e-3)
    parser.add_argument('--val_ratio', type=float, default=0.1)
    parser.add_argument('--criterion', type=str, default="cross_entropy") # 'cross_entropy', 'focal', 'label_smoothing', 'f1'
    parser.add_argument('--save_dir', type=str, default="./results")
    parser.add_argument('--report_name', type=str)
    parser.add_argument('--project_name', type=str, default="salt_v3")
    parser.add_argument('--token_type', type=str, default="origin") # 'origin', 'entity', 'type_entity', 'sub_obj', 'special_entity', 'special_type_entity'
    parser.add_argument('--wandb', type=str, default="True")
    parser.add_argument('--dropout', type=float, default=0.1)
    parser.add_argument('--sep_type', type=str, default='SEP') # SEP, ENT
    parser.add_argument('--lr_scheduler', type=str, default='reduceLR') # 'stepLR', 'reduceLR', 'cosine_anneal_warm', 'cosine_anneal', 'custom_cosine'
    parser.add_argument('--lstm_layers', type=int, default=2)
    parser.add_argument('--kfold_splits', type=int, default=5)
    
    args = parser.parse_args()
    main(args)
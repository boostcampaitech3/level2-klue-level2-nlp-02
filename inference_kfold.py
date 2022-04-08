import json
from gevent import config
from transformers import AutoTokenizer, AutoConfig, AutoModelForSequenceClassification, TrainingArguments, Trainer, AutoModel, ElectraTokenizer, ElectraForSequenceClassification, ElectraModel
from torch.utils.data import DataLoader
from utils.load_data import *
import pandas as pd
import torch
import torch.nn.functional as F

from functools import partial
import pickle as pickle
import numpy as np
import argparse
from tqdm import tqdm

def inference(model, tokenized_sent, device, tokenizer, model_type, batch_size):
    """
    test dataset을 DataLoader로 만들어 준 후,
    batch_size로 나눠 model이 예측 합니다.
    """
    dataloader = DataLoader(tokenized_sent, batch_size=batch_size, shuffle=False, collate_fn=partial(collate_fn, sep=tokenizer.sep_token_id))
    model.eval()
    output_pred = []
    output_prob = []
    for i, data in enumerate(tqdm(dataloader)):
        input_ids=data['input_ids'].to(device)
        attention_mask=data['attention_mask'].to(device)
        token_type_ids=data['token_type_ids'].to(device)
        with torch.no_grad():
            if 'xlm' in model_type:
                outputs = model(input_ids = input_ids, attention_mask=attention_mask)
            else:
                outputs = model(input_ids = input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)

        logits = outputs[0]
        output_prob.append(logits)

        # prob = F.softmax(logits, dim=-1).detach().cpu().numpy()
        # logits = logits.detach().cpu().numpy()
        # result = np.argmax(logits, axis=-1)

        # output_prob.append(result)
    output_prob = torch.cat(output_prob, dim=0)
    return output_prob


def num_to_label(label):
    """
    숫자로 되어 있던 class를 원본 문자열 라벨로 변환 합니다.
    """
    origin_label = []
    with open('./utils/dict_num_to_label.pkl', 'rb') as f:
        dict_num_to_label = pickle.load(f)
    for v in label:
        origin_label.append(dict_num_to_label[v])
    
    return origin_label

def load_test_dataset(dataset_dir, tokenizer, token_type, sep_type):
    """
    test dataset을 불러온 후,
    tokenizing 합니다.
    """
    test_dataset = load_data(dataset_dir, token_type)
    test_label = list(map(int,test_dataset['label'].values))
    # tokenizing dataset
    tokenized_test = tokenized_dataset(test_dataset, tokenizer, sep_type)
    return test_dataset['id'], tokenized_test, test_label

def main(args):
    """
    주어진 dataset csv 파일과 같은 형태일 경우 inference 가능한 코드입니다.
    """
    if not os.path.exists("./prediction"):
        os.mkdir("./prediction")
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

    MODEL_NAME = args.model_dir


    with open(os.path.join(MODEL_NAME, "model_config_parameters.json"), 'r') as f:
        mcp = json.load(f)
        token_type, sep_type, kfold_splits = mcp['token_type'], mcp['sep_type'], mcp['kfold_splits']
        model_case, use_lstm, model_type = mcp["model_case"], mcp["use_lstm"], mcp['model']

    # load tokenizer
    if model_case == 'automodel':
        tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
    elif model_case == 'electra':
        tokenizer = ElectraTokenizer.from_pretrained(MODEL_NAME)
    print('tokenizer', tokenizer)


    ## load test datset
    test_dataset_dir = "../dataset/test/test_data.csv"
    test_id, test_dataset, test_label = load_test_dataset(test_dataset_dir, tokenizer, token_type, sep_type)
    Re_test_dataset = RE_Dataset(test_dataset ,test_label)
    # get random number to choose example sentence
    data_idx = np.random.randint(0, 100)
    print("[dataset 예시]", tokenizer.decode(Re_test_dataset[data_idx]['input_ids']), sep='\n')

    probs = torch.zeros([7765, 30]).to(device)
    for kfold_idx in range(kfold_splits):
        model_path = os.path.join(args.model_dir, str(kfold_idx), args.select_model)
        if use_lstm:
            model = torch.load(os.path.join(model_path, 'model.bin'))
        else:
            if model_case == 'automodel':
                model = AutoModelForSequenceClassification.from_pretrained(model_path)
            elif model_case == 'electra':
                model = ElectraForSequenceClassification.from_pretrained(model_path)

        model.to(device)
        ## predict answer

        prob_answer  = inference(model, Re_test_dataset, device, tokenizer, model_type, args.batch_size) # model에서 class 추론
        probs += prob_answer

    probs = probs/kfold_splits
    probs = F.softmax(probs, dim=-1).detach().cpu().numpy()
    preds = np.argmax(probs, axis=-1)
    probs = probs.tolist()

    pred_answer = num_to_label(preds) # 숫자로 된 class를 원래 문자열 라벨로 변환.
    
    ## make csv file with predicted answer
    #########################################################
    # 아래 directory와 columns의 형태는 지켜주시기 바랍니다.
    output = pd.DataFrame({'id':test_id,'pred_label':pred_answer,'probs':probs,})

    output.to_csv('./prediction/' + args.submission_name + ".csv", index=False) # 최종적으로 완성된 예측한 라벨 csv 파일 형태로 저장.
    #### 필수!! ##############################################
    print('---- Finish! ----')
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    
    # model dir
    parser.add_argument('--model_dir', type=str, default="./results/best_loss")
    parser.add_argument('--submission_name', type=str, default="submission")
    parser.add_argument('--batch_size', type=int, default=62)
    parser.add_argument('--select_model', type=str, default='best_loss') # best_loss, best_fi

    args = parser.parse_args()
    print(args)
    main(args)
    
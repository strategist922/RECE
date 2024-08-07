from torchvision.models import vit_h_14, ViT_H_14_Weights, resnet50, ResNet50_Weights
from torchvision.io import read_image
from PIL import Image
import os, argparse
import torch
import pandas as pd
from tqdm import tqdm

if __name__=='__main__':
    parser = argparse.ArgumentParser(
                    prog = 'ImageClassification',
                    description = 'Takes the path of images and generates classification scores')
    parser.add_argument('--folder_path', help='path to images', type=str, required=True)
    parser.add_argument('--prompts_path', help='path to prompts', type=str, required=False, default='/vhome/gongchao/UCE_acceleration/adv-train/dataset/imagenette.csv')
    parser.add_argument('--target_class', help='target class', type=str, required=False, default='tench')
    parser.add_argument('--save_path', help='path to save results', type=str, required=False, default=None)
    parser.add_argument('--device', type=str, required=False, default='cuda:0')
    parser.add_argument('--topk', type=int, required=False, default=1)
    parser.add_argument('--batch_size', type=int, required=False, default=250)
    args = parser.parse_args()

    folder = args.folder_path
    topk = args.topk
    device = args.device
    batch_size = args.batch_size
    save_path = args.save_path
    prompts_path = args.prompts_path
    target_class = args.target_class
    if save_path is None:
        name_ = folder.split('/')[-1]
        save_path = f'{folder}/{name_}_classification.csv'
    weights = ResNet50_Weights.DEFAULT
    model = resnet50(weights=weights)
    model.to(device)
    model.eval()

    scores = {}
    categories = {}
    indexes = {}
    for k in range(1,topk+1):
        scores[f'top{k}']= []
        indexes[f'top{k}']=[]
        categories[f'top{k}']=[]

    names = os.listdir(folder)
    names = [name for name in names if '.png' in name or '.jpg' in name]
    if len(names) == 0:
        folder = folder+'/imgs'
        names = os.listdir(folder)
        names = [name for name in names if '.png' in name or '.jpg' in name]

    preprocess = weights.transforms()

    images = []
    for name in tqdm(names, desc='Reading images'):
        img = Image.open(os.path.join(folder,name))
        batch = preprocess(img)
        images.append(batch)

    if batch_size == None:
        batch_size = len(names)
    if batch_size > len(names):
        batch_size = len(names)
    images = torch.stack(images)
    # Step 4: Use the model and print the predicted category
    for i in tqdm(range(((len(names)-1)//batch_size)+1), desc='Classifying images'):
        batch = images[i*batch_size: min(len(names), (i+1)*batch_size)].to(device)
        with torch.no_grad():
            prediction = model(batch).softmax(1)
        probs, class_ids = torch.topk(prediction, topk, dim = 1)

        for k in range(1,topk+1):
            scores[f'top{k}'].extend(probs[:,k-1].detach().cpu().numpy())
            indexes[f'top{k}'].extend(class_ids[:,k-1].detach().cpu().numpy())
            categories[f'top{k}'].extend([weights.meta["categories"][idx] for idx in class_ids[:,k-1].detach().cpu().numpy()])

    if save_path is not None:
        df = pd.read_csv(prompts_path)
        df['case_number'] = df['case_number'].astype('int')
        case_numbers = []
        for i, name in enumerate(names):
            case_number = name.split('/')[-1].split('_')[0].replace('.png','').replace('.jpg','')
            case_numbers.append(int(case_number))
        dict_final = {'case_number': case_numbers}

        for k in range(1,topk+1):
            dict_final[f'category_top{k}'] = categories[f'top{k}'] 
            dict_final[f'index_top{k}'] = indexes[f'top{k}'] 
            dict_final[f'scores_top{k}'] = scores[f'top{k}'] 

        df_results = pd.DataFrame(dict_final)
        merged_df = pd.merge(df,df_results)
        merged_df.to_csv(save_path)

        target_acc = 0
        other_acc = 0        
        for i in range(len(merged_df)):
            if merged_df['category_top1'][i].lower() == merged_df['class'][i].lower() and merged_df['class'][i] == target_class.lower():
                target_acc += 1
            elif merged_df['category_top1'][i].lower() == merged_df['class'][i].lower() and merged_df['class'][i] != target_class.lower():
                other_acc += 1
        target_acc /= (len(merged_df)/10.)  # imagenette has 10 classes
        other_acc /= (9*len(merged_df)/10.)
        print(f'Target {target_class} accuracy: {target_acc}')
        print(f'Other accuracy: {other_acc}')

        # compute the accuracy of each class
        accuracies = {}
        for k in range(1,topk+1):
            accuracies[f'top{k}'] = {}
            for i in range(len(merged_df)):
                if merged_df[f'category_top{k}'][i].lower() == merged_df['class'][i].lower():
                    if merged_df['class'][i].lower() not in accuracies[f'top{k}']:
                        accuracies[f'top{k}'][merged_df['class'][i].lower()] = 0
                    accuracies[f'top{k}'][merged_df['class'][i].lower()] += 1

            for key in accuracies[f'top{k}']:
                accuracies[f'top{k}'][key] /= len(merged_df)/10.

        for k in range(1,topk+1):
            print(f'Top {k} accuracies:')
            for key in accuracies[f'top{k}']:
                print(f'{key}: {accuracies[f"top{k}"][key]}')

        

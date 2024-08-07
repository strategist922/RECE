import torch
import random
import pandas as pd
import argparse
import os
from diffusers import StableDiffusionPipeline, LMSDiscreteScheduler
from transformers import CLIPTokenizer
from functools import reduce
import operator
import time
import tqdm
import json
import numpy as np
import pickle

from erase_methods import edit_model_adversarial
from attack_methods import *
from execs import generate_images
from utils import generate_latents
from execs import compute_nudity_rate
from utils.embedding_calculation import close_form_emb, close_form_emb_regzero

def setup_seed(seed=123):
    random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.backends.cudnn.enabled = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
                    prog = 'TrainUSD',
                    description = 'Finetuning stable diffusion to debias the concepts')
    parser.add_argument('--concepts', help='concepts to erase', type=str, required=True)
    parser.add_argument('--old_target_concept', help='old target concept ever used in UCE', type=str, required=False, default=None)
    parser.add_argument('--seed', help='random seed', type=int, required=False, default=42)
    parser.add_argument('--epochs', help='epochs to train', type=int, required=False, default=10)
    parser.add_argument('--test_csv_path', help='path to csv file with prompts', type=str, default='dataset/nudity.csv')
    parser.add_argument('--guided_concepts', help='whether to use old prompts to guide', type=str, default=None)
    parser.add_argument('--preserve_concepts', help='whether to preserve old prompts', type=str, default=None)
    parser.add_argument('--technique', help='technique to erase (either replace or tensor)', type=str, required=False, default='replace')
    parser.add_argument('--base', help='base version for stable diffusion', type=str, required=False, default='1.4')
    parser.add_argument('--target_ckpt', help='target checkpoint to load, UCE', type=str, required=False, default='ckpt/unified-concept-editing/erased-nudity-towards_uncond-preserve_false-sd_1_4-method_replace-1-1.0.pt')
    parser.add_argument('--preserve_scale', help='scale to preserve concepts', type=float, required=False, default=0.1)
    parser.add_argument('--preserve_number', help='number of preserve concepts', type=int, required=False, default=None)
    parser.add_argument('--erase_scale', help='scale to erase concepts', type=float, required=False, default=1)
    parser.add_argument('--lamb', help='scale for init', type=float, required=False, default=0.1)
    parser.add_argument('--save_path', help='path to save the model', type=str, required=False, default='ckpt2/SD_adv_train')
    parser.add_argument('--concept_type', help='type of concept being erased', type=str, required=True)
    parser.add_argument('--emb_computing', help='close-form or gradient-descent, standard regularization or surrogate regularization', type=str, required=True, default='close_standardreg', choices=['close_standardreg', 'close_surrogatereg', 'close_regzero'])
    parser.add_argument('--reg_item', help='use 1st, 2nd or both items in surrogate regularization', type=str, required=False, default='1st', choices=['1st', '2nd','both'])
    parser.add_argument('--regular_scale', help='scale for regularization', type=float, required=False, default=1e-3)
    parser.add_argument('--num_samples', help='number of samples for gradient descent', type=int, required=False, default=1)
    parser.add_argument('--ddim_steps', help='number of steps for ddim', type=int, required=False, default=50)

    args = parser.parse_args()
    seed_shuffle=123
    setup_seed(seed_shuffle)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    concepts = args.concepts.split(',')
    concepts = [con.strip() for con in concepts]

    if args.old_target_concept is None:
        old_target_concept = [None for _ in concepts]
    else:
        old_target_concept = args.old_target_concept.split(',')
        old_target_concept = [con.strip() for con in old_target_concept]
        for idx, con in enumerate(old_target_concept):
            if con == 'none':
                old_target_concept[idx] = None
            if con == '':
                old_target_concept[idx] = ' '
    assert len(old_target_concept) == len(concepts), f'length of old_target_concept {len(old_target_concept)} should be the same as concepts {len(concepts)}'

    seed = args.seed
    epochs = args.epochs
    guided_concepts = args.guided_concepts
    preserve_concepts = args.preserve_concepts
    technique = args.technique
    preserve_scale = args.preserve_scale
    erase_scale = args.erase_scale
    lamb = args.lamb
    preserve_number = args.preserve_number
    concept_type = args.concept_type
    emb_computing = args.emb_computing
    reg_item = args.reg_item
    regular_scale = args.regular_scale
    num_samples = args.num_samples
    ddim_steps = args.ddim_steps
    sd14="/share/ckpt/gongchao/model_zoo/models--CompVis--stable-diffusion-v1-4/snapshots/133a221b8aa7292a167afc5127cb63fb5005638b"
    sd21='/share/ckpt/gongchao/model_zoo/models--stabilityai--stable-diffusion-2-1-base/snapshots/5ede9e4bf3e3fd1cb0ef2f7a3fff13ee514fdf06'
    if args.base=='1.4':
        model_version = sd14
    elif args.base=='2.1':
        model_version = sd21
    else:
        model_version = sd14
    ldm_stable = StableDiffusionPipeline.from_pretrained(
        model_version,
        )
    ldm_stable_copy = StableDiffusionPipeline.from_pretrained(
        model_version,
        )
    ldm_stable = ldm_stable.to(device)
    ldm_stable_copy = ldm_stable_copy.to(device)
    tokenizer = CLIPTokenizer.from_pretrained(model_version, subfolder="tokenizer")

    target_ckpt = args.target_ckpt
    dev_df = pd.read_csv(args.test_csv_path)

    print_text=''
    for concept in concepts:
        print_text+=f'{concept}_'

    # PROMPT CLEANING
    if concepts[0] == 'allartist':
        concepts = ["Kelly Mckernan", "Thomas Kinkade", "Pablo Picasso", "Tyler Edlin", "Kilian Eng"]
    if concepts[0] == '10artists':
        concepts = ["Asger Jorn", "Eric Fischl", "Johannes Vermeer", "Apollinary Vasnetsov", "Naoki Urasawa", "Nicolas Mignard", "John Whitcomb", "John Constable", "Warwick Globe", "Albert Marquet"]

    if 'artists' in concepts[0]:
        df = pd.read_csv('dataset/artists1734_prompts.csv')
        artists = list(df.artist.unique())
        number = int(concepts[0].replace('artists', ''))
        concepts = random.sample(artists,number) 

    # create a new df similar to prompts_df, using concepts and seed
    # It should contain prompt, evaluation_seed
    adv_df = pd.DataFrame(columns=['prompt', 'evaluation_seed'])
    for concept in concepts:
        adv_df = adv_df.append({'prompt':concept, 'evaluation_seed':args.seed}, ignore_index=True)


    old_texts = []
    for concept in concepts:
        old_texts.append(f'{concept}')
    
    if guided_concepts is None:
        new_texts = [' ' for _ in old_texts]
        print_text+=f'-towards_uncond'
    else:
        guided_concepts = [con.strip() for con in guided_concepts.split(',')]
        if len(guided_concepts) == 1:
            new_texts = [guided_concepts[0] for _ in old_texts]
            print_text+=f'-towards_{guided_concepts[0]}'
        else:
            new_texts = [[con] for con in guided_concepts]
            new_texts = reduce(operator.concat, new_texts)
            print_text+=f'-towards'
            for t in new_texts:
                if t not in print_text:
                    print_text+=f'-{t}'
            
    assert len(new_texts) == len(old_texts)
    
    
    if preserve_concepts is None:
        if concept_type == 'art':
            prompts_df = pd.read_csv('dataset/artists1734_prompts.csv')

            retain_texts = list(prompts_df.artist.unique())
            old_texts_lower = [text.lower() for text in old_texts]
            preserve_concepts = [text for text in retain_texts if text.lower() not in old_texts_lower]
            if preserve_number is not None:
                print_text+=f'-preserving_{len(old_texts)}artists'
                preserve_concepts = random.sample(preserve_concepts, len(old_texts))
        else:
            preserve_concepts = []
    if type(preserve_concepts) == str:
        preserve_concepts = [con.strip() for con in preserve_concepts.split(',')]
    retain_texts = ['']+preserve_concepts
    if len(retain_texts) > 1:
        print_text+=f'-preserve_true'     
    else:
        print_text+=f'-preserve_false'
    if preserve_scale is None:
        # set the format to be .3f
        preserve_scale = max(0.1, 1/len(retain_texts))
        preserve_scale = round(preserve_scale, 3)

    print_text += f"-sd_{args.base.replace('.','_')}" 
    print_text += f"-method_{technique}" 
    print_text += f"-erase_{erase_scale}"
    print_text += f"-preserve_{preserve_scale}"
    print_text += f"-lamb_{lamb}"
    print_text = print_text.lower()
    print(print_text)
    
    
    if 'close' in emb_computing:
        if 'surrogate' in emb_computing:
            save_path = f'{args.save_path}/{concept_type}/{emb_computing}_regitem_{reg_item}/{print_text}/regular_{regular_scale}/seed_{seed}'
        else:
            save_path = f'{args.save_path}/{concept_type}/{emb_computing}/{print_text}/regular_{regular_scale}/seed_{seed}'
    os.makedirs(save_path, exist_ok=True)

    generate_images(ldm_stable, dev_df, f'{save_path}/before', ddim_steps=ddim_steps, num_samples=num_samples)
    # load UCE model
    if target_ckpt != '':
        ldm_stable.unet.load_state_dict(torch.load(target_ckpt))
    ldm_stable.to(device)
    generate_images(ldm_stable, dev_df, f'{save_path}/uce', ddim_steps=ddim_steps, num_samples=num_samples)

    start = time.time()

    for epoch in tqdm.tqdm(range(epochs), desc='Epoch'):
        adv_emb_list = []
        new_emb_list = []
        for i in range(0, len(old_texts)):
            # batch size is 1
            batch_df = adv_df.iloc[i:i+1]
            batch_concept = old_texts[i]
            batch_old_target_concept = old_target_concept[i]
            batch_new_text = new_texts[i]
            
            # tokenize
            id_concept = tokenizer(batch_concept, padding="max_length", max_length=tokenizer.model_max_length, truncation=True, return_tensors="pt").input_ids.to(device)
            id_new_text = tokenizer(batch_new_text, padding="max_length", max_length=tokenizer.model_max_length, truncation=True, return_tensors="pt").input_ids.to(device)
            
            # get embeddings
            input_embedding = ldm_stable.text_encoder(id_concept)[0]
            new_embedding = ldm_stable.text_encoder(id_new_text)[0]
            new_emb_list.append(new_embedding[0])   # squeeze the batch dimension
            
            input_ids = id_concept
            if 'close' in emb_computing:
                if 'surrogate' in emb_computing:
                    _, adv_embedding = close_form_emb(ldm_stable, ldm_stable_copy, batch_concept, with_to_k=True, save_path=save_path, old_target_concept=None, regeular_scale=regular_scale, seed=seed, save_name=f'{epoch}-{i}', reg_item=reg_item)
                elif 'standard' in emb_computing:
                    _, adv_embedding = close_form_emb(ldm_stable, ldm_stable_copy, batch_concept, with_to_k=True, save_path=save_path, old_target_concept=batch_old_target_concept, regeular_scale=regular_scale, seed=seed, save_name=f'{epoch}-{i}')
                elif 'regzero' in emb_computing:
                    _, adv_embedding = close_form_emb_regzero(ldm_stable, ldm_stable_copy, batch_concept, with_to_k=True, save_path=save_path, regeular_scale=regular_scale, seed=seed, save_name=f'{epoch}-{i}')
            else:
                raise NotImplementedError
            adv_emb_list.append(adv_embedding[0])   # squeeze the batch dimension
        ldm_stable = edit_model_adversarial(ldm_stable, adv_emb_list, new_emb_list, retain_texts, technique=technique, preserve_scale=preserve_scale, erase_scale=erase_scale, lamb=lamb)
        generate_images(ldm_stable, dev_df, f'{save_path}/epoch_{epoch}', ddim_steps=ddim_steps, num_samples=num_samples)
        torch.save(ldm_stable.unet.state_dict(), f'{save_path}/epoch_{epoch}.pt')
    end = time.time()
    print(f'Running time: {end-start}')
    print(f'Running time per epoch: {(end-start)/epochs}')

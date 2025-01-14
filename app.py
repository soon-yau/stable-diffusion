import streamlit as st
st.set_page_config(layout="wide")

import os
import pickle
from PIL import Image
from pathlib import Path
from shutil import copy, rmtree
import pandas as pd
import numpy as np
from glob import glob
from copy import deepcopy
import math
import time
import torch
from torchvision import transforms as T
from einops import rearrange
from omegaconf import OmegaConf
from ldm.data.generate_utils import InferenceModel, draw_styles, convert_fname, interp_mask

DEVICE = 'cuda:0'
CONFIG_FILE = 'models/upgpt/interp_256/config.yaml'
CKPT = 'models/upgpt/interp_256/upgpt.interp256.v1.ckpt'
upscale_ckpt = "models/upgpt/upscale/upgpt.upscale.v1.ckpt"

styles_root = Path('styles')
cache_root = Path('app_cache')
local_style_root = cache_root/'styles'
local_pose_root = cache_root/'pose'
local_lowres_root = cache_root/'samples_lowres'
local_interp_root = cache_root/'interp'

os.makedirs(local_style_root, exist_ok=True)
os.makedirs(local_lowres_root, exist_ok=True)
os.makedirs(local_interp_root, exist_ok=True)
pose_folders = sorted([x[0] for x in os.walk(local_pose_root)][1:])
pose_images = []
for pose_folder in pose_folders:
    pose_images.append(Image.open(glob(os.path.join(pose_folder,'*.jpg'))[0]))
#pose_images = [Image.open(Path(x)/'pose.jpg') for x in pose_folders]

def delete_files_in_folder(folder_path):
    for file_name in os.listdir(folder_path):
        file_path = os.path.join(folder_path, file_name)
        if os.path.isfile(file_path):
            os.remove(file_path)


def clear_image_cache():
    for x in glob(str(local_lowres_root/'*')):
        os.remove(x)

def get_image_number():
    image_files = [os.path.split(x)[1] for x in glob(str(local_lowres_root/'*.jpg'))]
    if len(image_files) == 0:
        return 0

    fnames = [f.split('_')[-1].split('.jpg')[0] for f in image_files]
    file_id = max([int(f) for f in fnames if f.isnumeric()])
    #file_id = max([int(f.split('_')[1].split('.jpg')[0]) for f in image_files])
    
    return '{:03d}'.format(file_id + 1)

def get_samples(folder):
    print(folder, sorted(glob(str(folder/'*.jpg'))))
    return [Image.open(x) for x in sorted(glob(str(folder/'*.jpg')))]

map_df = pd.read_csv("data/deepfashion/deepfashion_map.csv")
map_df.set_index('image', inplace=True)

st.title('UPGPT - Person Image Generation, Edit, Pose Transfer and Pose Interpolation')
style_names = ['face', 'hair', 'headwear', 'background', 'top', 'outer', 'bottom', 'shoes', 'accesories']


clip_norm = T.Normalize(mean=(0.48145466, 0.4578275, 0.40821073), 
                            std=(0.26862954, 0.26130258, 0.27577711))
clip_transform = T.Compose([
    T.ToTensor(),
    clip_norm])

smpl_image_transform = T.CenterCrop(size=(256, 192))

mask_transform = T.Compose([
    T.Resize(size=[32, 24], interpolation=T.InterpolationMode.NEAREST),
    T.ToTensor(),
    T.Lambda(lambda x: x * 2. - 1.)
])

image_transform = T.Compose([
    T.ToTensor(),
    T.Lambda(lambda x: rearrange(x * 2. - 1., 'c h w -> h w c'))])

lr_transform = T.Compose([
    T.Pad((4,0),padding_mode='edge'),
    T.Resize(size=[128, 96], interpolation=T.InterpolationMode.BILINEAR),
    T.ToTensor(),
    T.Lambda(lambda x: x * 2. - 1.,)])

@st.cache_resource
def upgpt_model(config_file, ckpt, device):    
    config = OmegaConf.load(config_file)
    model = InferenceModel(config, ckpt, device)

    return model 

model = upgpt_model(CONFIG_FILE, CKPT, DEVICE)

disable_upscale = True
if os.path.exists(upscale_ckpt):
    upscale_model = upgpt_model('models/upgpt/upscale/config.yaml',
                                upscale_ckpt, device=DEVICE)

    disable_upscale = False

def load_smpl(folder):
    smpl_file = glob(str(Path(folder)/'*.p'))[0]
    smpl_image_file = glob(str(Path(folder)/'*.jpg'))[0]
    input_mask_type = 'mask'
    #pose_path = str(Path(folder)/'pose')
    #smpl_image_file = pose_path + '.jpg'
    #smpl_file = pose_path + '.p'
    smpl_image = smpl_image_transform(Image.open(smpl_image_file))
    if input_mask_type=='mask':
        mask_file = glob(str(Path(folder)/'*.png'))[0]
        #mask_file = pose_path + '_mask.png'
        mask_image = Image.open(mask_file)
        person_mask = mask_transform(mask_image)
    elif input_mask_type=='bbox':
        raise "Not supported"
    else:
        person_mask = mask_transform(smpl_image)
       
    with open(smpl_file, 'rb') as f:
        smpl_params = pickle.load(f)
        pred_pose = smpl_params[0]['pred_body_pose']
        pred_betas = smpl_params[0]['pred_betas']
        pred_camera = np.expand_dims(smpl_params[0]['pred_camera'], 0)
        smpl_pose = np.concatenate((pred_pose, pred_betas, pred_camera), axis=1)
        smpl_pose = T.ToTensor()(smpl_pose).view((1,-1))

    return {'smpl':smpl_pose, 
            'smpl_image':smpl_image, 
            'person_mask':person_mask}

def get_styles(input_style_names=style_names):
    style_images = []
    for style_name in input_style_names:
        f_path = local_style_root/f'{style_name}.jpg'

        if f_path.exists():
            style_image = clip_transform((Image.open(f_path)))
        else:
            style_image = clip_norm(torch.zeros(3, 224, 224))
        style_images.append(style_image)
    style_images = torch.stack(style_images)  
    return style_images

def get_coord(batch_mask):
    mask = batch_mask[0].cpu().numpy()
    mask[mask==-1] = 0
    x = np.nonzero(np.mean(mask,1))[0]
    xmin, xmax = x[0], x[-1]
    y = np.nonzero(np.mean(mask,0))[0]
    ymin, ymax = y[0], y[-1]

    return np.array([xmin, xmax, ymin, ymax])

def get_mask(mask, coord):
    xmin, xmax, ymin, ymax = coord
    new_mask = np.ones_like(mask.cpu().numpy())*(-1)
    new_mask[0,xmin:xmax+1, ymin:ymax+1] = -0.99215686
    #return new_mask
    return torch.tensor(new_mask).to(mask.device)

def interp_mask(src_mask, dst_mask, alpha):    
    coord_1 = get_coord(src_mask)
    coord_2 = get_coord(dst_mask)

    coord = (alpha * coord_1 + (1 - alpha) * coord_2).astype(np.int32)
    #print(coord)
    #coord = np.array([ 0, 31,  12, 19])
    new_mask = get_mask(src_mask, coord)
    return new_mask



left_column, mid_column, right_column = st.columns([1,1,3])

# right column
right_column.markdown("##### Generated Images")
gen_image = right_column.empty()

def display_samples(folder, loc=gen_image):
    global image_ids
    low_res_images = get_samples(folder)
    image_ids = [i+1 for i in range(len(low_res_images))]
    loc.image(low_res_images, width=192, caption=image_ids)

with right_column:
    
    show_image_button = right_column.button(label='Show images')

    if show_image_button:
        display_samples(local_lowres_root)

    image_files = sorted(glob(str(local_lowres_root/'*.jpg')))
    delete_ids = [i+1 for i in range(len(image_files))]
    
    del_options = st.multiselect('Select images to delete', delete_ids, [])
    clear_image_button = st.button(label='Delete images')    
    if clear_image_button:
        for del_option in del_options:
            os.remove(image_files[del_option-1])
        #clear_image_cache()
        #gen_image.empty()
    delete_all_gen_button = st.button(label='Delete all generated images')
    if delete_all_gen_button:
        delete_files_in_folder(str(local_lowres_root    ))        
    display_samples(local_lowres_root)

right_column.markdown("##### Pose Interpolation")
interp_factors = right_column.text_input('Interplation factor, from 1.0 to 0.0, comma seperated. You may need to tweak the spacing for better result.', 
                            value='1.0, 0.8, 0.7, 0.6, 0.4, 0.3, 0.2, 0.1, 0.0')
interp_image = right_column.empty()
delete_all_interp_button = right_column.button(label='Delete all images')
if delete_all_interp_button:
    delete_files_in_folder(str(local_interp_root))
display_samples(local_lowres_root)
display_samples(local_interp_root, interp_image)


with left_column:
    with st.form(key='input'):    
        st.markdown("##### Content Text")
        default_text = "a woman is wearing a long sleeve shirt and long pant."
        content_text = st.text_area('Content text', label_visibility='hidden', value=default_text)
        st.markdown("##### Style Text")
        
        style_columns = st.columns(3)

        style_texts = []
        for i, style in enumerate(style_names):
            col = i//3
            with style_columns[col]:
                style_texts.append(st.text_input(style))
        st.markdown("##### Pose")
        pose_ids = [i+1 for i in range(len(pose_images))]
        st.image(pose_images, caption=pose_ids, width=96)
        pose_column_1, pose_column_2 = st.columns([1,1])
        with pose_column_1:
            pose_select = st.radio("Source pose", pose_ids, index=0)
        with pose_column_2:
            target_pose_select = st.radio("Target pose", pose_ids, index=3)
        st.markdown("---")
        
        gen_column, interp_column = st.columns([1,1])
        with gen_column:
            submit_button = st.form_submit_button(label='Generate')
        with interp_column:
            interp_button = st.form_submit_button(label='Interpolate')

        if submit_button:
            style_features = get_styles()
            batch = {}
            style_texts_dict = dict(zip(style_names, style_texts))
            batch['image'] = image_transform(Image.open(cache_root/'image_256.jpg')) # dummy
            batch['styles'] = model.mix_style(style_features, style_texts_dict)
            batch['txt'] = content_text
            
            batch.update(load_smpl(pose_folders[pose_select - 1]))
            batch = model.create_batch(batch, repeat=1)
            log = model.generate(batch, 200)
            sample = Image.fromarray(np.uint8(log['samples'][0]*255))
            sample.save(local_lowres_root/f'sample_{get_image_number()}.jpg')
            display_samples(local_lowres_root)
            # gen_image.image(get_samples(), width=192)
            # low_res_images = get_samples()
            # image_ids = [i+1 for i in range(len(low_res_images))]

        if interp_button:
            delete_files_in_folder(str(local_interp_root))
            style_features = get_styles()
            batch = {}
            style_texts_dict = dict(zip(style_names, style_texts))
            batch['image'] = image_transform(Image.open(cache_root/'image_256.jpg')) # dummy
            batch['styles'] = model.mix_style(style_features, style_texts_dict)
            batch['txt'] = content_text
            
            dst_batch = deepcopy(batch)
            src_pose = load_smpl(pose_folders[pose_select - 1])
            dst_pose = load_smpl(pose_folders[target_pose_select - 1])
            batch.update(src_pose)
            dst_batch.update(dst_pose)

            alphas = np.array([float(num) for num in interp_factors.split(',')])
            batch = model.create_batch(batch, repeat=len(alphas))
            
            for i, alpha in enumerate(alphas):
                batch['smpl'][i] = alpha * batch['smpl'][i] + (1 - alpha) * dst_batch['smpl'].to(DEVICE)
                batch['person_mask'][i] = interp_mask(batch['person_mask'][i], dst_batch['person_mask'], alpha)
            log = model.generate(batch, 200)
            
            for i, sample in enumerate(log['samples']):
                sample = Image.fromarray(np.uint8(sample*255))
                sample.save(local_interp_root/f'interp_{i}.jpg')
                #interp_images.append(sample)
            time.sleep(1) # wait to save file into disk
            display_samples(local_interp_root, interp_image)
            #gen_image.image(interp_images, width=192)

with mid_column:
    #left_2_column, right_2_column = st.columns([1,1])
    #style_image = right_2_column.empty()
    #with left_2_column:
    with st.form("my-form", clear_on_submit=False):
        st.markdown("##### Style Images")
        style_file = st.file_uploader("Style reference")
        style_image = st.empty()
        options = None
        if style_file is not None:
            style_local_fname = style_file.name.replace('-','/')
            row = map_df.loc[style_local_fname]
            style_path = row.styles
            bytes_data = style_file.read()
            style_image.image(bytes_data, width=128)
            options = st.multiselect('Select styles', style_names, [])
            style_file = None
        style_button = st.form_submit_button(label='Show/Get Styles')
        #clear_style_button = st.form_submit_button(label='Clear Styles')

        if style_button:
            #style_image.empty()
            if options:
                for opt in options:
                    src = styles_root/style_path/f'{opt}.jpg'
                    if src.exists():
                        dst = local_style_root/f'{opt}.jpg'
                        copy(src, dst)
                
            for style in style_names:
                dst = local_style_root/f'{style}.jpg'
                if dst.exists():
                    st.image(Image.open(dst), width=128, caption=style)

        styles_to_delete = []   
        for style in style_names:
            dst = local_style_root/f'{style}.jpg'
            if dst.exists():
                styles_to_delete.append(style)                
                #os.remove(dst)

        del_options = st.multiselect('Select styles to delete', styles_to_delete, styles_to_delete)
        del_style_button = st.form_submit_button(label='Remove Styles')
        if del_style_button:
            for style in del_options:
                dst = local_style_root/f'{style}.jpg'
                os.remove(dst)

                

# with right_column:
    
#     show_image_button = right_column.button(label='Show images')

#     if show_image_button:
#         display_samples(local_lowres_root)

#     image_files = sorted(glob(str(local_lowres_root/'*.jpg')))
#     delete_ids = [i+1 for i in range(len(image_files))]
#     del_options = st.multiselect('Select images to delete', delete_ids, [])
#     clear_image_button = st.button(label='Delete images')
#     if clear_image_button:
#         for del_option in del_options:
#             os.remove(image_files[del_option-1])
#         #clear_image_cache()
#         #gen_image.empty()
#     display_samples(local_lowres_root)

with right_column:
    st.markdown('#####  Upscale')
    c1, c2 = st.columns([1,1])
    with c1:
        upscale_folder = st.selectbox('Generated/Interpolated', ['Generated','Interpolated'], label_visibility='hidden')
    with c2:
        upscale_select = st.selectbox('Upscale', image_ids, label_visibility='hidden')    
    upscale_button = st.button(label='Upscale', disabled=disable_upscale)
    if upscale_button:
        folder = local_interp_root if upscale_folder == 'Interpolated' else local_lowres_root
        low_res_images = get_samples(folder)
        style_features = get_styles(style_names)
        batch = {}
        batch['image'] = image_transform(Image.open(cache_root/'image_512.jpg')) # dummy
        lr_image = low_res_images[upscale_select - 1]
        batch['lr'] = lr_transform(lr_image)
        batch['styles'] = model.mix_style(style_features, {})
        batch['txt'] = content_text
        batch = model.create_batch(batch, repeat=1)
        log = upscale_model.generate(batch, use_ema=False)
        sample = Image.fromarray(np.uint8(log['samples'][0]*255))
        st.image(sample, width=384)
        fname = cache_root/'sample_512.png'
        sample.save(fname)
        with open(str(fname), "rb") as file:
            btn = st.download_button(
                    label="Download image",
                    data=file,
                    file_name="download.png",
                    mime="image/png"
                )
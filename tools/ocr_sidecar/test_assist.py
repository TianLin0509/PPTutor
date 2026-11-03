"""Run in the OCR build environment, which already supplies OpenCV/NumPy."""
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).parent))
from assist import cutout


def test_white_glyphs_removed_without_erasing_red_card(tmp_path):
    source = tmp_path / 'source.png'
    image = Image.new('RGB', (500, 200), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyhbd.ttc', 36)
    box = list(draw.textbbox((100, 60), '模态演进', font=font))
    draw.rectangle((90, box[1]-3, box[2]+10, box[3]+3), fill='#B50605')
    draw.text((100, 60), '模态演进', font=font, fill='white')
    image.save(source)
    result = cutout({'image':str(source),'outputDir':str(tmp_path/'out'), 'elements':[
        {'id':'s1','type':'text','bbox':box,'color':'#FFFFFF','text':'模态演进'}]})
    rebuilt = Image.open(result['clean']).convert('RGB')
    sample = np.array(rebuilt.crop(tuple(box)))
    assert (sample[:,:,1] > 220).sum() < 10, 'white glyph remnants must be removed'
    assert np.mean(sample[:,:,0]) > 150, 'the red card must survive'
    assert rebuilt.getpixel((20,20)) == (255,255,255)


def test_graphics_recompose_exactly_and_are_removed_from_base(tmp_path):
    source = tmp_path / 'source.png'
    image = Image.new('RGB', (300, 200), 'white')
    ImageDraw.Draw(image).rectangle((40,40,139,119),fill='#3478F6')
    image.save(source)
    result = cutout({'image':str(source),'outputDir':str(tmp_path/'out'),'elements':[
        {'id':'g1','type':'graphic','bbox':[40,40,140,120]}]})
    base = Image.open(result['base']).convert('RGB')
    assert base.getpixel((60,60)) == (255,255,255)
    base.paste(Image.open(result['graphics'][0]['asset']), (40,40))
    assert np.array_equal(np.array(base),np.array(image))


def test_empty_ops_contours_are_explicit_review_items(tmp_path):
    source = tmp_path / 'source.png'
    image = Image.new('RGB',(300,200),'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((40,40,80,100),fill='red')
    draw.rectangle((160,40,220,100),fill='blue')
    image.save(source)
    result = cutout({'image':str(source),'outputDir':str(tmp_path/'out'),'elements':[], 'autoGraphics':True})
    assert len(result['graphics']) == 2
    assert len(result['reviewItems']) == 2

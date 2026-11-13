from pathlib import Path
import json
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageFilter
from rembg import remove, new_session

ROOT=Path(__file__).resolve().parent
KIT=ROOT/'source/RHPS_Theater_Kit/RHPS Theater Kit'
OUT=ROOT/'transparent_png'
OUT.mkdir(exist_ok=True)
manifest=[]

def save(image,name,source,method):
    image=image.convert('RGBA')
    bbox=image.getchannel('A').getbbox()
    if bbox: image=image.crop(bbox)
    target=OUT/name
    image.save(target,optimize=True)
    alpha=image.getchannel('A')
    manifest.append({'file':str(target.relative_to(ROOT)),'source':str(source.relative_to(ROOT)),'method':method,'width':image.width,'height':image.height,'alpha_extrema':alpha.getextrema(),'transparent_fraction':round(1-alpha.histogram()[255]/(image.width*image.height),3)})
    print(name,image.size,'alpha',alpha.getextrema(),flush=True)

for source in (KIT/'Logos').glob('*.png'):
    image=Image.open(source)
    save(image,source.name,source,'original high-resolution theater-kit logo (preserved alpha)')

session=new_session('u2netp')
for stem in ['Columbia','FrankFloorshow','FrankGirls','FrankThrone','FrankBradJanet','LipsAlt']:
    source=KIT/'Photos'/f'RHPSPhoto_{stem}.jpg'
    image=Image.open(source).convert('RGB')
    scale=min(1,1600/max(image.size))
    small=image.resize((round(image.width*scale),round(image.height*scale)),Image.Resampling.LANCZOS)
    cut=remove(small,session=session,alpha_matting=False,post_process_mask=True)
    alpha=cut.getchannel('A').resize(image.size,Image.Resampling.LANCZOS)
    alpha=alpha.filter(ImageFilter.GaussianBlur(0.7))
    image.putalpha(alpha)
    save(image,f'{stem}_auto.png',source,'u2netp automatic foreground mask; draft matte, inspect edges/limbs before show use')

(ROOT/'cutout_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Generated',len(manifest),'transparent PNGs')

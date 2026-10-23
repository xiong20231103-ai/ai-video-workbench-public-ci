"""Real-video acceptance runner. No production services or allocation writes.

python generate_cover_v2_evidence.py --sources sources.json --output evidence
sources.json maps each material ID to its own verified real video path.
"""
import argparse
import hashlib
import json
import subprocess
import struct
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont,ImageOps
import board_video_stitcher as s
import cover_templates_v2 as v

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def glyphs(path):
    data=Path(path).read_bytes();count=struct.unpack_from('>H',data,4)[0]
    offset=None
    for i in range(count):
        tag,_,start,_=struct.unpack_from('>4sIII',data,12+i*16)
        if tag==b'cmap':offset=start
    if offset is None:raise ValueError('font_cmap_missing')
    n=struct.unpack_from('>H',data,offset+2)[0];supported=set()
    for i in range(n):
        platform,encoding,relative=struct.unpack_from('>HHI',data,offset+4+i*8)
        if platform not in (0,3):continue
        start=offset+relative;fmt=struct.unpack_from('>H',data,start)[0]
        if fmt==4:
            segments=struct.unpack_from('>H',data,start+6)[0]//2
            ends=start+14;begins=ends+2*segments+2;deltas=begins+2*segments;ranges=deltas+2*segments
            for j in range(segments):
                end=struct.unpack_from('>H',data,ends+j*2)[0];begin=struct.unpack_from('>H',data,begins+j*2)[0]
                delta=struct.unpack_from('>h',data,deltas+j*2)[0];shift=struct.unpack_from('>H',data,ranges+j*2)[0]
                for code in range(begin,min(end,65534)+1):
                    gid=(code+delta)&65535 if shift==0 else struct.unpack_from('>H',data,ranges+j*2+shift+2*(code-begin))[0]
                    if gid:supported.add(code)
        elif fmt==12:
            groups=struct.unpack_from('>I',data,start+12)[0]
            for j in range(groups):
                a,b,g=struct.unpack_from('>III',data,start+16+j*12)
                supported.update(range(a if g else a+1,b+1))
    return supported

def sheet(rows,path,labels,cell=(180,320)):
    cw,ch=cell;canvas=Image.new('RGB',(cw*len(rows[0]),(ch+28)*len(rows)),'#e9e3d9');draw=ImageDraw.Draw(canvas)
    font=ImageFont.truetype(v.font_path('en'),12)
    for y,row in enumerate(rows):
        for x,p in enumerate(row):
            with Image.open(p) as image:
                image=image.convert('RGB');image.thumbnail((cw,ch));canvas.paste(image,(x*cw+(cw-image.width)//2,y*(ch+28)+28))
            draw.text((x*cw+3,y*(ch+28)+4),labels[y][x],font=font,fill='#554d43')
    canvas.save(path)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--sources',required=True);parser.add_argument('--output',required=True);parser.add_argument('--ffmpeg',default=r'C:\ffmpeg\bin\ffmpeg.exe');args=parser.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);sources=json.loads(Path(args.sources).read_text(encoding='utf-8-sig'))
    if set(sources)!=set(s.MATERIALS):raise ValueError('must provide exactly nine material sources')
    frames={};manifest={};results=[];errors=[];fontmaps={}
    for m,video in sources.items():
        frame=out/f'{m}-first-frame.png'
        subprocess.run([args.ffmpeg,'-hide_banner','-loglevel','error','-i',str(video),'-frames:v','1','-y',str(frame)],check=True,timeout=60)
        frames[m]=frame;manifest[m]={'video':str(video),'videoSha256':sha(video),'firstFrameSha256':sha(frame)}
    for width,height in ((1080,1920),(1440,2560)):
        for m,frame in frames.items():
            base=out/f'{m}-{width}-base.png'
            with Image.open(frame) as image:
                # Match production normalization to vertical canvas before template crop.
                image=ImageOps.pad(image.convert('RGB'),(width,height),method=Image.Resampling.LANCZOS,color='black');image.save(base)
            for language in s.LANGUAGES:
                copy=s.localized_copy(m,language)
                for t in v.IDS:
                    dest=out/'renders'/str(width)/f'{m}-{language}-{t}.png';dest.parent.mkdir(parents=True,exist_ok=True)
                    config={**copy,'material_id':m,'language_code':language,'cover_template_id':t}
                    try:
                        report=s.render_cover_frame(base,Path(s.__file__).parent/'ExampleBrandC-transparent.png',config,dest)
                        for e in report['elements']:
                            if 'font' not in e:continue
                            cmap=fontmaps.setdefault(e['font'],None)
                            if cmap is None:cmap=glyphs(e['font']);fontmaps[e['font']]=cmap
                            missing={c for line in e['lines'] for c in line if not c.isspace() and ord(c) not in cmap}
                            if missing:raise ValueError(f'font_glyph_missing: {e["font"]}: {missing}')
                        results.append({'file':str(dest),'sha256':sha(dest),**report})
                    except Exception as exc:errors.append({'material':m,'language':language,'template':t,'size':width,'error':str(exc)})
            print('matrix',width,m,'errors',len(errors),flush=True)
    # ExampleBrandB long-language coverage, separate from 1080 canonical matrix.
    for t in v.IDS:
        for language in ('de','ru','ko'):
            m='wood_veneer';config={**s.localized_copy(m,language),'material_id':m,'language_code':language,'cover_template_id':t}
            dest=out/f'ExampleBrandB-{language}-{t}.png'
            try:results.append({'file':str(dest),**s.render_cover_frame(out/f'{m}-1080-base.png',Path(s.__file__).parent/'ExampleBrandB-transparent.png',config,dest)})
            except Exception as exc:errors.append({'logo':'ExampleBrandB','language':language,'template':t,'error':str(exc)})
    rows=[];labels=[]
    for m in s.MATERIALS:
        row=[out/'renders/1080'/f'{m}-en-{t}.png' for t in v.IDS]
        if all(p.exists() for p in row):rows.append(row);labels.append([f'{m} / {t}' for t in v.IDS])
    if rows:sheet(rows,out/'all-materials-en.jpg',labels)
    crop_rows=[];crop_labels=[]
    for m in s.MATERIALS:
        row=[frames[m]];names=['original first frame']
        for t in ('old_a',v.IDS[0],v.IDS[2],v.IDS[3]):
            matches=[r for r in results if r.get('template')==t and r.get('size')==(1080,1920) and Path(r['file']).name==f'{m}-en-{t}.png']
            box=(7,52,78,33) if t=='old_a' else matches[0]['photo_box'] if matches else ((7,7,36,78) if t==v.IDS[2] else v.LAYOUTS[t]['photo'])
            with Image.open(out/f'{m}-1080-base.png') as base:
                photo,pos,_=v.crop_photo(v.filtered_photo(base,False,t),box,(55,47) if t=='old_a' else v.LAYOUTS[t]['focus'],1080,1920)
            canvas=Image.new('RGB',(1080,1920),'#E9E3D9');canvas.paste(photo,pos)
            path=out/f'crop-{m}-{t}.png';canvas.save(path);row.append(path)
            names.append('warm A before (33%H)' if t=='old_a' else t+(' [text blocked]' if not matches else ''))
        crop_rows.append(row);crop_labels.append([f'{m} / {x}' for x in names])
    if crop_rows:sheet(crop_rows,out/'crop-check.jpg',crop_labels,cell=(240,426))
    longrows=[];longlabels=[]
    for l in ('de','ru','ko'):
        row=[out/'renders/1080'/f'wood_veneer-{l}-{t}.png' for t in v.IDS]
        if all(p.exists() for p in row):longrows.append(row);longlabels.append([f'{l} / {t}' for t in v.IDS])
    if longrows:sheet(longrows,out/'long-titles.jpg',longlabels)
    sheet([[out/'renders/1080'/f'wood_veneer-en-{t}.png' for t in v.IDS]],out/'six-template-comparison.jpg',[list(v.IDS)],cell=(240,426))
    for brand in ('ExampleBrandC','ExampleBrandB'):
        rows=[];labels=[]
        for m in s.MATERIALS:
            c={**s.localized_copy(m,'en'),'material_id':m,'language_code':'en','cover_template_id':'cover_b_v2'}
            dest=out/f'B-logo-{brand}-{m}.png'
            try:
                report=s.render_cover_frame(out/f'{m}-1080-base.png',Path(s.__file__).parent/f'{brand}-transparent.png',c,dest)
                results.append({'file':str(dest),**report})
                # Show complete B and a 2x logo-region detail beside it.
                detail=out/f'B-logo-detail-{brand}-{m}.png'
                with Image.open(dest) as im:im.crop((0,0,round(im.width*.40),round(im.height*.24))).save(detail)
                rows.append([dest,detail]);labels.append([f'{m} / {brand} / B',f'{m} / paper + gray logo'])
            except Exception as exc:errors.append({'logo':brand,'material':m,'template':'cover_b_v2','error':str(exc)})
        if rows:sheet(rows,out/f'b-logo-{brand.lower()}.jpg',labels,cell=(240,426))
    (out/'evidence.json').write_text(json.dumps({'sources':manifest,'results':results,'errors':errors,'fonts':list(fontmaps),'koEnglishFallbackMaterials':['plastic_wood_flooring','crystal_rock_wall']},ensure_ascii=False,indent=2),encoding='utf-8')
    print('renders',len(results),'errors',len(errors))
    if errors:raise SystemExit(1)

if __name__=='__main__':main()

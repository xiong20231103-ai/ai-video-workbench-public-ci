"""Versioned, percentage-based cover layouts; deliberately independent of A–D.

Coordinates are percentages of W/H. CSS filter matrices operate in sRGB,
clamping after each filter. Geometry diagnostics use actual glyph ink bounds.
"""
import contextlib
import hashlib
import json
import math
import os
import struct
from pathlib import Path
import tempfile
import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageFilter

IDS = tuple(f'cover_{x}_v2' for x in 'abcd') + ('champagne_gold_a_v2', 'champagne_gold_e_v2')
ROTATION = IDS[:4]
NAMES = dict(zip(IDS, ('暖灰画册·上题下图', '暖灰画册·暖纸叠题', '暖灰画册·窄栏杂志', '暖灰画册·上图下签', '香槟黑金·影像渐隐', '香槟黑金·影像落版')))
LAYOUTS = {
 IDS[0]: dict(photo=(7,40,78,45),focus=(55,47),logo=(7,7,15,8.4375),category=(7,18,78,3),title=(7,21,78,10),subtitle=(7,32,78,6),sizes=(10,4,2.9,2.35),leading=(.98,1.35)),
 IDS[1]: dict(photo=(0,0,100,100),focus=(54,48),panel=(7,60,78,28),logo=(8.5,7.84375,18,10.125),category=(11,64,70,3),title=(11,69,70,10),subtitle=(11,81,68,6),sizes=(9.2,4,2.9,2.35),leading=(.98,1.35)),
 IDS[2]: dict(photo=(7,7,49,78),focus=(50,48),logo=(62,9,16,9),category=(62,40,22,3),title=(62,46,22,18),subtitle=(62,68,22,12),sizes=(7.1,3.2,2.6,2.05),leading=(1.04,1.45)),
 IDS[3]: dict(photo=(7,7,78,55),focus=(56,46),logo=(7,65,15,8.4375),category=(29,65,56,3),title=(29,70,56,10),subtitle=(7,82,78,6),sizes=(8.8,4,2.7,2.15),leading=(.98,1.35)),
 IDS[4]: dict(photo=(0,0,100,100),focus=(53,50),logo=(7,8,16,9),category=(7,60,50,3),title=(7,68,76,10),subtitle=(7,80,74,7),rule=(7,65,10,.0521),folio=(72,61,12,2.5),sizes=(10,4,2.9,2.65),leading=(.98,1.38)),
 IDS[5]: dict(photo=(7,7,78,55),focus=(53,47),panel=(7,58,78,30),logo=(8,9,15,8.4375),category=(12,65,50,3),title=(12,73,67,7.5),subtitle=(12,82,67,5.5),rule=(12,70,9,.0521),folio=(71,65,12,2.5),sizes=(9,4,2.9,2.2),leading=(.98,1.25)),
}

def font_path(language, bold=False):
    name = ('malgunbd.ttf' if bold else 'malgun.ttf') if language == 'ko' else ('georgiab.ttf' if bold else 'georgia.ttf')
    path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / name
    if not path.is_file():
        raise ValueError(f'cover_font_missing: {path}')
    return str(path)

@lru_cache(maxsize=8)
def font_codepoints(path):
    """Read Unicode cmap (formats 4/12); reject missing glyphs before drawing."""
    data=Path(path).read_bytes()
    for i in range(struct.unpack_from('>H',data,4)[0]):
        tag,_,offset,_=struct.unpack_from('>4sIII',data,12+i*16)
        if tag==b'cmap':break
    else:raise ValueError(f'cover_font_cmap_missing: {path}')
    supported=set()
    for i in range(struct.unpack_from('>H',data,offset+2)[0]):
        platform,_,relative=struct.unpack_from('>HHI',data,offset+4+i*8)
        if platform not in (0,3):continue
        start=offset+relative;fmt=struct.unpack_from('>H',data,start)[0]
        if fmt==4:
            n=struct.unpack_from('>H',data,start+6)[0]//2
            ends=start+14;begins=ends+2*n+2;deltas=begins+2*n;ranges=deltas+2*n
            for j in range(n):
                end=struct.unpack_from('>H',data,ends+2*j)[0];begin=struct.unpack_from('>H',data,begins+2*j)[0]
                delta=struct.unpack_from('>h',data,deltas+2*j)[0];shift=struct.unpack_from('>H',data,ranges+2*j)[0]
                for code in range(begin,min(end,65534)+1):
                    gid=(code+delta)&65535 if not shift else struct.unpack_from('>H',data,ranges+2*j+shift+2*(code-begin))[0]
                    if gid:supported.add(code)
        elif fmt==12:
            for j in range(struct.unpack_from('>I',data,start+12)[0]):
                a,b,g=struct.unpack_from('>III',data,start+16+j*12)
                supported.update(range(a if g else a+1,b+1))
    return frozenset(supported)

def rect(box, w, h):
    x,y,bw,bh=box
    return (round(x*w/100),round(y*h/100),round(bw*w/100),round(bh*h/100))

def filtered_photo(source, gold, template):
    # Image.convert's affine matrix matches CSS sRGB filter arithmetic.
    img=source.convert('RGB')
    def saturation(value):
        nonlocal img
        lum=(.213,.715,.072)
        matrix=[]
        for i in range(3):
            matrix.extend([(1-value)*lum[j]+(value if i==j else 0) for j in range(3)]+[0])
        img=img.convert('RGB',tuple(matrix))
    if not gold:
        saturation(.78)  # grayscale(.22)
        saturation(.56)
        brightness,contrast=.88,.94
    else:
        saturation(.67 if template==IDS[4] else .70)
        brightness,contrast=(.83,1.02) if template==IDS[4] else (.90,1)
    img=img.point([min(255,round(i*brightness)) for i in range(256)]*3)
    img=img.point([max(0,min(255,round((i-127.5)*contrast+127.5))) for i in range(256)]*3)
    return img

def crop_photo(source, box, focus, w,h):
    x,y,bw,bh=rect(box,w,h)
    sw,sh=source.size
    ratio=max(bw/sw,bh/sh)
    cw,ch=bw/ratio,bh/ratio
    # CSS object-position: align the corresponding percentages of excess.
    left=(sw-cw)*focus[0]/100;top=(sh-ch)*focus[1]/100
    crop=(left,top,left+cw,top+ch)
    return source.crop(crop).resize((bw,bh),Image.Resampling.LANCZOS),(x,y),crop

@lru_cache(maxsize=65536)
def glyph(path,size,char):
    font=cached_font(path,size)
    return font.getlength(char),font.getbbox(char,anchor='ls')

@lru_cache(maxsize=512)
def cached_font(path,size):
    return ImageFont.truetype(path,size)

def tracked_width(draw,text,font,tracking):
    return sum(glyph(font.path,font.size,c)[0] for c in text)+max(0,len(text)-1)*tracking

def line_options(text):
    flat=' '.join(text.split())
    choices=[]
    # Prefer word/hyphen boundaries; character splitting only for CJK text.
    for i in range(1,len(flat)):
        if flat[i-1].isspace() or flat[i-1] in '-–' or ('\uac00'<=flat[i-1]<='\ud7a3'):
            a,b=flat[:i].strip(),flat[i:].strip()
            if a and b: choices.append((a,b))
    return flat, choices

def three_line_options(flat):
    breaks=[i for i in range(1,len(flat)) if flat[i-1].isspace() or flat[i-1] in '-–' or '\uac00'<=flat[i-1]<='\ud7a3']
    return [(flat[:a].strip(),flat[a:b].strip(),flat[b:].strip()) for ai,a in enumerate(breaks) for b in breaks[ai+1:] if flat[a:b].strip()]

def fit_text(text,box,maximum,minimum,w,h,path,tracking_em,leading,max_lines=2):
    missing={c for c in text if not c.isspace() and ord(c) not in font_codepoints(path)}
    if missing:raise ValueError(f'cover_font_glyph_missing: {path}: {sorted(missing)}')
    draw=ImageDraw.Draw(Image.new('L',(1,1)))
    flat,breaks=line_options(text)
    _,_,bw,bh=rect(box,w,h)
    lower=math.ceil(minimum*w/100)
    upper=math.floor(maximum*w/100)
    for lines_count in range(1,max_lines+1):
        def evaluate(size):
            font=cached_font(path,size)
            track=size*tracking_em
            candidates=[(flat,)] if lines_count==1 else breaks if lines_count==2 else three_line_options(flat)
            candidates=sorted(candidates,key=lambda lines: max(tracked_width(draw,s,font,track) for s in lines))
            for lines in candidates:
                widths=[tracked_width(draw,s,font,track) for s in lines]
                # Include each glyph's ink overhang, not just advance widths.
                bounds=[]
                for j,line in enumerate(lines):
                    cursor=0.; ink=[]
                    for char in line:
                        advance,b=glyph(path,size,char)
                        ink.append((cursor+b[0],j*size*leading+b[1],cursor+b[2],j*size*leading+b[3]));cursor+=advance+track
                    bounds.append((min(b[0] for b in ink),min(b[1] for b in ink),max(b[2] for b in ink),max(b[3] for b in ink)))
                left=min(b[0] for b in bounds);top=min(b[1] for b in bounds)
                right=max(b[2] for b in bounds);bottom=max(b[3] for b in bounds)
                if max(widths)<=bw and right-left<=bw and bottom-top<=bh:
                    # Ensure actual line ink does not collide.
                    if any(a[3]>b[1] for a,b in zip(bounds,bounds[1:])):continue
                    return dict(lines=lines,size=size,font=font,tracking=track,leading=leading,ink=(left,top,right,bottom),path=path)
            return None
        best=evaluate(lower)
        if best is None:continue
        low,high=lower+1,upper
        while low<=high:
            mid=(low+high)//2
            candidate=evaluate(mid)
            if candidate is not None:best=candidate;low=mid+1
            else:high=mid-1
        return best
    raise ValueError(f'cover_text_overflow: {text!r}; box={box}; min={minimum}%W')

def prepare_text(layout,config,category,title,w,h,gold):
    language=str(config.get('language_code','en'));ko=language=='ko'
    maximum,minimum,catmax,submax=layout['sizes'];lh,slh=layout['leading']
    sub=str(config.get('subtitle','')).strip()
    if not sub:
        heights={IDS[0]:18,IDS[1]:17,IDS[2]:31,IDS[3]:17,IDS[4]:18,IDS[5]:14}
        x,y,bw,_=layout['title'];layout['title']=(x,72 if config['cover_template_id']==IDS[5] else y,bw,heights[config['cover_template_id']])
    specs={
      'category':(category,catmax,2.2,True,0 if ko else (.14 if gold else .16),1.05),
      'title':(title,maximum,minimum,gold,0 if ko else (-.022 if gold else -.025),lh),
      'subtitle':(sub,submax,1.9,True,0 if ko else (.075 if gold else .10),slh),
    }
    result={}
    for key,(text,hi,lo,bold,tracking,leading) in specs.items():
        if text:
            # User-approved amendment: C benefit copy may use
            # three lines; titles and all other templates stay at two.
            lines=3 if key=='subtitle' and config['cover_template_id']==IDS[2] else 2
            result[key]=fit_text(text,layout[key],hi,lo,w,h,font_path(language,bold),tracking,leading,lines)
    return result

def render(base_frame,logo_path,config,output_path,category,title):
    template=config['cover_template_id'];gold=template.startswith('champagne_')
    layout=dict(LAYOUTS[template]);expanded=False
    with Image.open(base_frame) as source:photo=source.convert('RGB')
    w,h=photo.size
    try:texts=prepare_text(layout,config,category,title,w,h,gold)
    except ValueError as exc:
        if not str(exc).startswith('cover_text_overflow') or template!=IDS[2]:raise
        expanded=True
        layout.update(photo=(7,7,36,78),logo=(47,9,18,10.125),category=(47,40,38,3),title=(47,46,38,18),subtitle=(47,68,38,12))
        texts=prepare_text(layout,config,category,title,w,h,gold)
    if template==IDS[5] and len(texts['title']['lines'])==2 and str(config.get('subtitle','')).strip():
        layout.update(title=(12,72,67,9.5),subtitle=(12,83,67,4.5))
        texts=prepare_text(layout,config,category,title,w,h,gold)
    canvas=Image.new('RGBA',(w,h),'#25231F' if gold else '#E9E3D9')
    image,pos,crop=crop_photo(filtered_photo(photo,gold,template),layout['photo'],layout['focus'],w,h)
    canvas.paste(image,pos)
    if template==IDS[4]:
        grad=Image.new('RGBA',(1,h),(37,35,31,0))
        for y in range(h):
            p=y*100/h
            alpha=0 if p<=42 else (round((p-42)/(66.36-42)*189) if p<=66.36 else round(189+(p-66.36)/(100-66.36)*66))
            grad.putpixel((0,y),(37,35,31,max(0,min(255,alpha))))
        canvas=Image.alpha_composite(canvas,grad.resize((w,h)))
    if 'panel' in layout:
        x,y,bw,bh=rect(layout['panel'],w,h)
        color=(37,35,31,255) if gold else (233,227,217,239)
        layer=Image.new('RGBA',(w,h));ImageDraw.Draw(layer).rectangle((x,y,x+bw-1,y+bh-1),fill=color)
        canvas=Image.alpha_composite(canvas,layer)
    with Image.open(logo_path) as source:logo=source.convert('RGBA')
    bbox=logo.getchannel('A').getbbox()
    if bbox is None:raise ValueError('cover_logo_empty')
    logo=logo.crop(bbox)
    x,y,bw,bh=rect(layout['logo'],w,h)
    if 'baijax' in Path(logo_path).name.lower():
        cap=22 if template==IDS[2] and not expanded else 18 if template in (IDS[0],IDS[3]) else 24
        bw=round(w*cap/100);bh=round(h*.055)
    scale=min(bw/logo.width,bh/logo.height)
    logo=logo.resize((max(1,round(logo.width*scale)),max(1,round(logo.height*scale))),Image.Resampling.LANCZOS)
    if not gold:
        # Tint after resampling: avoid premultiplied-alpha rounding RGB 97 to 96.
        alpha=logo.getchannel('A').point(lambda a:round(a*224/255))
        logo=Image.new('RGBA',logo.size,(97,97,97,0));logo.putalpha(alpha)
    if gold:
        shadow=Image.new('RGBA',(w,h));mark=Image.new('RGBA',logo.size,(0,0,0,0))
        mark.putalpha(logo.getchannel('A').point(lambda a:round(a*136/255)))
        shadow.alpha_composite(mark,(x,y+round(h*.001)))
        canvas=Image.alpha_composite(canvas,shadow.filter(ImageFilter.GaussianBlur(w*.006)))
    logo_bounds=(x,y,x+logo.width,y+logo.height)
    if template==IDS[1]:
        padding=round(w*.015)
        logo_bounds=(x-padding,y-padding,x+logo.width+padding,y+logo.height+padding)
        paper=Image.new('RGBA',(logo.width+2*padding,logo.height+2*padding),(233,227,217,239))
        canvas.alpha_composite(paper,(x-padding,y-padding))
    canvas.alpha_composite(logo,(x,y))
    ink=logo.getchannel('A').getbbox()
    mark_ink=(x+ink[0],y+ink[1],x+ink[2],y+ink[3])
    elements=[dict(kind='logo',bounds=logo_bounds,mark_bounds=(x,y,x+logo.width,y+logo.height),ink_bounds=mark_ink,paper=template==IDS[1])]
    if 'rule' in layout:
        x,y,bw,bh=rect(layout['rule'],w,h)
        line=Image.new('RGBA',(bw,max(1,bh)),(181,155,106,209));canvas.alpha_composite(line,(x,y))
    draw=ImageDraw.Draw(canvas)
    colors={'title':'#EEE9DF' if gold else '#554D43','category':'#C8B58D' if gold else '#887C6D','subtitle':'#D1C9BB' if gold else '#756B60'}
    if 'folio' in layout:
        texts['folio']=fit_text('01 / A' if template==IDS[4] else '05 / E',layout['folio'],2.5,2.5,w,h,font_path(config.get('language_code','en'),True),.12,1,1)
        colors['folio']='#C4B99F'
    for key,item in texts.items():
        x,y,bw,bh=rect(layout[key],w,h);il,it,ir,ib=item['ink']
        if key=='folio':x+=bw-(ir-il)
        origin_x=x-il;origin_y=y-it
        for j,line in enumerate(item['lines']):
            cursor=origin_x
            for char in line:
                draw.text((cursor,origin_y+j*item['size']*item['leading']),char,font=item['font'],fill=colors[key],anchor='ls')
                cursor+=draw.textlength(char,font=item['font'])+item['tracking']
        elements.append(dict(kind=key,bounds=(x,y,x+ir-il,y+ib-it),font=item['path'],size=item['size'],lines=list(item['lines'])))
    if template==IDS[0]:
        category_ink=next(e['bounds'] for e in elements if e['kind']=='category')
        gap=category_ink[1]-elements[0]['ink_bounds'][3]
        if gap<h*.02:
            raise ValueError(f'cover_a_logo_category_gap_violation: {gap}px < {h*.02}px')
    # Fail closed: all text/logo ink must fit, and different elements cannot overlap.
    for e in elements:
        a,b,c,d=e['bounds']
        if a<w*.07-1 or b<h*.07-1 or c>w*.85+1 or d>h*.88+1:raise ValueError(f'cover_safe_area_violation: {e}')
    for i,a in enumerate(elements):
        for b in elements[i+1:]:
            ax,ay,ar,ab=a['bounds'];bx,by,br,bb=b['bounds']
            if min(ar,br)>max(ax,bx) and min(ab,bb)+h*.008>max(ay,by):
                raise ValueError(f'cover_element_overlap: {a["kind"]}/{b["kind"]}')
    canvas.convert('RGB').save(output_path,'PNG',compress_level=2)
    report=dict(template=template,size=(w,h),expanded=expanded,photo_crop=crop,photo_box=layout['photo'],elements=elements)
    if config.get('cover_diagnostics_path'):
        Path(config['cover_diagnostics_path']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report

@contextlib.contextmanager
def ledger_lock(path,timeout=5):
    import msvcrt
    deadline=time.monotonic()+timeout
    with open(path,'a+b') as f:
        if f.tell()==0:f.write(b'0');f.flush()
        while True:
            try:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1);break
            except OSError:
                if time.monotonic()>=deadline:raise TimeoutError('cover_v2_ledger_lock_busy')
                time.sleep(.05)
        try:yield
        finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

def persist_assignment(root,identity,key,assignment):
    """A separate ledger; never calls the legacy allocation writer."""
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/(hashlib.sha256(identity.encode('utf-8')).hexdigest()+'.json')
    with ledger_lock(path.with_suffix('.lock')):
        data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schemaVersion':2,'identity':identity,'assignments':{}}
        if data.get('schemaVersion')!=2 or data.get('identity')!=identity or not isinstance(data.get('assignments'),dict):raise ValueError('cover_v2_ledger_invalid')
        previous=data['assignments'].get(key)
        if previous:
            if previous.get('coverTemplateId') not in IDS:raise ValueError('cover_v2_assignment_invalid')
            return previous
        data['assignments'][key]=assignment
        fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=root)
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        return assignment

"""Run durable Junfei image jobs through the current authenticated Ego page."""
import argparse,json,uuid,subprocess,shutil,os,tempfile,runpy
from pathlib import Path

def write_json_atomic(path,value):
    path=Path(path)
    descriptor,temporary=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    except BaseException:
        try:os.unlink(temporary)
        except FileNotFoundError:pass
        raise

def main():
    p=argparse.ArgumentParser(description='钧飞工作台三功能：原模板套图、修图、原始GLSL换色；复用当前浏览器登录')
    p.add_argument('--job',type=Path,required=True,help='JSON: {jobs:[{productId,mode,source,brief,...}]}')
    p.add_argument('--space',type=int,required=True,help='当前任务的Ego TaskSpace ID')
    p.add_argument('--page',default='p1')
    p.add_argument('--base',default='http://127.0.0.1:'+os.environ.get('JUNFEI_PORT','18769'))
    p.add_argument('--output',type=Path,required=True);p.add_argument('--timeout',type=int,default=1800)
    args=p.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    root=Path(os.environ.get('JUNFEI_PROJECT_ROOT') or Path(__file__).resolve().parents[4]/'studio-dev')
    active_space=runpy.run_path(str(root/'automation/browser_space_lock.py'))['active_space']
    with active_space(0,root=output), active_space(args.space):
        run(args,output)


def run(args,output):
    descriptor=output/'workbench-jobs.json'
    previous=json.loads(descriptor.read_text('utf-8')) if descriptor.exists() else None
    data=json.loads(args.job.resolve().read_text('utf-8'))
    jobs=data.get('jobs')
    if not isinstance(jobs,list) or not jobs:raise ValueError('job需要非空jobs列表')
    for index,job in enumerate(jobs):
        existing=(previous or {}).get('jobs',[])
        frozen=index<len(existing) and bool(existing[index].get('payload'))
        if job.get('mode') not in ('suite','repair','colour') or not job.get('productId') or not job.get('brief'):raise ValueError('每项需要productId/mode/brief')
        for field in ('source','maskFile','targetReferenceFile'):
            if job.get(field):
                path=Path(job[field]).expanduser().resolve()
                if not frozen and not path.is_file():raise ValueError('图片不存在：'+str(path))
                job[field]=str(path)
        association=job.get('variantSource')
        if association is not None:
            if (not isinstance(association,dict) or set(association)!={'colorKey','reference'}
                    or not all(isinstance(v,str) and v.strip() for v in association.values())
                    or job['mode']=='colour' or job.get('reusePlateTaskId')):
                raise ValueError('variantSource 须提供已关联颜色的 colorKey 和 reference，适用于套图或生成式颜色变体')
            if bool(job.get('generatedColourVariant')) != bool(job.get('source')):
                raise ValueError('套图直接用关联原图；生成式颜色变体需另提供母版 source')
            if job.get('mode')!='suite' and not job.get('generatedColourVariant'):
                raise ValueError('只有套图和生成式颜色变体支持关联颜色来源')
        elif not job.get('source'):
            raise ValueError('每项需要 source 本地原图或 variantSource')
        if job.get('generatedColourVariant') and not association and not frozen:
            raise ValueError('生成式颜色变体需要 variantSource 关联真实目标颜色')
        extra=job.get('referenceFiles',[])
        if not isinstance(extra,list) or len(extra)>2:raise ValueError('referenceFiles最多2张，与source合计不超过3张')
        if association and job.get('generatedColourVariant') and extra:
            raise ValueError('生成式颜色变体仅使用母版与关联目标颜色来源')
        resolved=[]
        for filename in extra:
            path=Path(filename).expanduser().resolve()
            if not frozen and not path.is_file():raise ValueError('补充原图不存在：'+str(path))
            resolved.append(str(path))
        if 'referenceFiles'in job:job['referenceFiles']=resolved
    if previous:
        if previous['inputs']!=jobs or previous['base']!=args.base.rstrip('/'):raise ValueError('此输出目录已绑定不同输入，请用新的版本目录')
        previous.update(space=args.space,page=args.page,timeout=args.timeout)
        write_json_atomic(descriptor,previous)
    else:
        write_json_atomic(descriptor,{'base':args.base.rstrip('/'),'space':args.space,'page':args.page,'output':str(output),
            'timeout':args.timeout,'inputs':jobs,'jobs':[{'input':j,'requestId':uuid.uuid4().hex} for j in jobs]})
    # One fresh module per invocation; the durable descriptor carries resume state.
    wrapper=output/('run-'+uuid.uuid4().hex+'.mjs')
    helper=(Path(__file__).parent/'workbench_images.mjs').resolve().as_uri()
    wrapper.write_text('const {runJobs}=await import('+json.dumps(helper)+');\nawait runJobs('+json.dumps(str(descriptor))+');\n','utf-8')
    entry="await import('"+wrapper.as_uri()+"')"
    executable=shutil.which('ego-browser')
    if executable:command=[executable,'nodejs','-e',entry]
    else:
        # This Windows installation exposes ego-browser through its shell profile.
        shell=shutil.which('pwsh') or shutil.which('powershell')
        if not shell:raise RuntimeError('未找到ego-browser；按ego-browser技能安装CLI后重试')
        command=[shell,'-NoLogo','-Command','ego-browser nodejs -e "'+entry+'"']
    subprocess.run(command,check=True)
if __name__=='__main__':main()

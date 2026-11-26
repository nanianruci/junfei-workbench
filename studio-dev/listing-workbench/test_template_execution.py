import tempfile,unittest,uuid,hashlib,json,copy
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import app_paths,image_engine as engine,apparel_suite as apparel

class TemplateExecutionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
  for mod,key,val in [(app_paths,'DATA',root),(engine,'DB',root/'engine.sqlite3')]:
   p=patch.object(mod,key,val);p.start();self.addCleanup(p.stop)
  self.source=engine.cache(Image.new('RGB',(100,100),'navy'))['url']
  self.art=engine.cache(Image.new('RGB',(100,100),'white'))['url']
  raw='完整模板：上方大商品图，下方两张特写及标题。'
  self.template={'id':'123','style':'C','name':'original','url':self.art,'sha256':engine.asset(self.art)[1],
   'prompt':raw,'promptSha256':hashlib.sha256(raw.encode()).hexdigest()}
 def test_textless_apparel_detail_submits_real_template_instead_of_crop(self):
  recipe=apparel.freeze_request({'counts':{'seller':1}},[self.source],reference_sha256=[engine.asset(self.source)[1]],
   templates=[self.template],count=1,no_text=True,brief='按模板展示真实领口')
  task={'id':uuid.uuid4().hex,'productId':'p','results':[], 'request':{'mode':'suite','references':[self.source],
   'officialTemplates':[self.template],'apparelSuite':recipe,'brief':recipe['brief'],'count':1,'noText':True,
   'templateExecutionPolicy':'official-artwork-v1'}}
  proposal={'identity':'navy top','style':{},'shots':[{'kind':'seller','layout':'detail','sourceIndex':0,
   'question':'领口','evidence':'原图可见','scene':'template grouping','camera':'front','copy':[],
   'sourceCrop':[.1,.1,.6,.6]}]}
  def paid(task,prompt,refs,**kwargs):
   self.assertIn(self.template['prompt'],prompt);self.assertEqual(refs,[self.source,self.art])
   return {'index':0,'providerJobId':'paid-test','image':self.source,'raw':self.source,'aspectValid':True}
  review={'blocking':False,'identityPreserved':True,'templateFollowed':True,'issues':[],'warnings':[]}
  with patch.object(engine,'get',return_value=task),patch.object(engine,'assert_assets'),patch.object(engine,'save'),\
       patch.object(engine,'json_model',side_effect=[proposal,review])as vision,patch.object(engine,'paid_image',side_effect=paid)as submit:
   engine.process(task['id'])
  self.assertEqual(task['status'],'completed',task.get('error'));submit.assert_called_once()
  receipt=task['results'][0]['templateExecution']
  self.assertTrue(receipt['artworkReferenceSubmitted']);self.assertTrue(receipt['completeOriginalPromptSubmitted'])
  self.assertEqual(vision.call_args.args[2][-1],self.art)
 def test_generic_image_cannot_pass_new_template_review(self):
  task={'request':{'mode':'suite','brief':'保留模板版式','templateExecutionPolicy':'official-artwork-v1'}}
  result={'image':self.source,'shot':{'officialTemplate':self.template}}
  with patch.object(engine,'save'),patch.object(engine,'json_model',return_value={'blocking':False,'identityPreserved':True,'templateFollowed':False,'issues':[]}):
   engine.review(task,result,self.source)
  self.assertTrue(result['review']['blocking'])
 def test_missing_template_verdict_is_not_a_pass(self):
  task={'request':{'mode':'suite','brief':'模板','templateExecutionPolicy':'official-artwork-v1'}}
  result={'image':self.source,'shot':{'officialTemplate':self.template}}
  with patch.object(engine,'save'),patch.object(engine,'json_model',return_value={'blocking':False,'identityPreserved':True}):
   with self.assertRaisesRegex(ValueError,'模板执行检查'):engine.review(task,result,self.source)
 def test_returned_rich_plan_normalizes_without_changing_geometry_or_copy(self):
  source_crop={'x':.24,'y':.09,'width':.55,'height':.59}
  shot={'layout':'detail','sourceIndex':0,'question':'What does the edge look like?',
        'scene':{'composition':'Show the edge close up'},'camera':'Front detail',
        'evidence':{'visible':['Visible stitching'],'detailCrops':[{'feature':'stitching','sourceIndex':0,'sourceCrop':source_crop.copy()}]},
        'copy':['Visible stitching'],'sourceCrop':source_crop}
  raw={'identity':'Synthetic reference garment','style':{},'shots':[copy.deepcopy(shot) for _ in range(3)]}
  before=copy.deepcopy(raw);result=engine.normalize_shots(copy.deepcopy(raw),3,1,False)
  self.assertEqual(raw,before)
  self.assertEqual(result['shots'][2]['sourceCrop'],[.24,.09,.55,.59])
  self.assertEqual(result['shots'][0]['copy'],before['shots'][0]['copy'])
  self.assertEqual(result['shots'][0]['scene'],before['shots'][0]['scene']['composition'])
  bad=copy.deepcopy(raw);bad['shots'][0]['evidence']['detailCrops'][0]['sourceCrop']['width']=1
  with self.assertRaisesRegex(ValueError,'超出原片'):engine.normalize_shots(bad,3,1,False)
 def test_plan_recovery_requires_same_frozen_intent_and_no_image_submit(self):
  req={'mode':'suite','brief':'same','count':3,'noText':False,'referenceSha256':['a'*64],
   'officialTemplates':[self.template],'apparelSuite':{'references':[{'url':'/old.png','sha256':'a'*64,'role':'primary'}]}}
  parent={'id':'old','productId':'p','status':'failed','request':req,'results':[],
   'error':'结构化可见证据必须包含真实文字条目','plannerProposal':{'shots':[]}}
  new=copy.deepcopy(req);new['apparelSuite']['references'][0]['url']='/new.png'
  recovered=engine.frozen_plan_recovery(new,parent,'p');self.assertEqual(recovered['parentTaskId'],'old')
  for field,value in [('status','submission_unknown'),('providerJobId','already-submitted'),('pending',{'prompt':'intent'})]:
   invalid={**parent,field:value}
   with self.assertRaises(ValueError):engine.frozen_plan_recovery(new,invalid,'p')
  for key,value in [('brief','changed'),('referenceSha256',['b'*64])]:
   with self.assertRaises(ValueError):engine.frozen_plan_recovery({**new,key:value},parent,'p')
  invalid=copy.deepcopy(new);invalid['officialTemplates'][0]['promptSha256']='f'*64
  with self.assertRaises(ValueError):engine.frozen_plan_recovery(invalid,parent,'p')
 def test_generation_recovery_verifies_completed_original_provider_asset(self):
  rawsha=engine.asset(self.source)[1]
  req={'mode':'suite','brief':'same','count':1,'noText':False,'referenceSha256':[rawsha],
   'officialTemplates':[self.template],'apparelSuite':{}}
  row={'index':0,'providerJobId':'done','raw':self.source,'rawSha256':rawsha,'image':self.source}
  parent={'id':'old','productId':'p','status':'failed','error':'衣料边界与独立衣料证据仍冲突，不能自动换色',
   'request':req,'plan':{'identity':'top','shots':[]},'results':[row],'pending':{'acknowledged':'done'}}
  with patch('image_provider.poll',return_value={'status':'completed','images':[self.source]}):
   recovered=engine.frozen_generation_recovery(req,parent,'p')
  self.assertTrue(recovered['providerCompletionsVerified']);self.assertFalse(recovered['paidImageSubmissionReplayed'])
  with patch('image_provider.poll',return_value={'status':'submission_unknown','images':[]}):
   with self.assertRaises(ValueError):engine.frozen_generation_recovery(req,parent,'p')
 def test_completed_generation_reuse_does_not_submit_or_reinterpret_prompt(self):
  prior={'index':0,'prompt':'exact','references':[self.source,self.art],'image':self.source,'raw':self.source}
  task={'results':[],'request':{'reusedGeneration':{'parentTaskId':'parent','results':[prior]}}}
  with patch('image_provider.submit')as submit,patch.object(engine,'require_codex')as config:
   reused=engine.paid_image(task,'exact',[self.source,self.art])
   submit.assert_not_called();config.assert_not_called()
   self.assertEqual(reused['reusedFromTaskId'],'parent')
   with self.assertRaisesRegex(ValueError,'未重发生图'):engine.paid_image(task,'changed',[self.source,self.art])
 def test_conflicting_optional_template_grade_retains_native_and_still_requires_full_review(self):
  for blocking in [False,True]:
   with self.subTest(blocking=blocking):
    recipe=apparel.freeze_request({'counts':{'seller':1}},[self.source],reference_sha256=[engine.asset(self.source)[1]],
     templates=[self.template],count=1,no_text=False,brief='真实模板')
    task={'id':uuid.uuid4().hex,'productId':'p','results':[], 'request':{'mode':'suite','references':[self.source],
     'officialTemplates':[self.template],'apparelSuite':recipe,'brief':recipe['brief'],'count':1,'noText':False,
     'templateExecutionPolicy':'official-artwork-v1','fidelityPolicy':{'garmentKind':'upper'}}}
    proposal={'identity':'navy top','style':{},'shots':[{'kind':'seller','layout':'feature','sourceIndex':0,
     'question':'shape','evidence':'source visible','scene':'original template grouping','camera':'front','copy':['Ribbed Knit']}]}
    review={'blocking':blocking,'identityPreserved':True,'templateFollowed':True,'issues':['wrong colour']if blocking else[]}
    native={'index':0,'providerJobId':'completed','image':self.source,'raw':self.source,'aspectValid':True}
    with patch.object(engine,'get',return_value=task),patch.object(engine,'assert_assets'),patch.object(engine,'save'),\
         patch.object(engine,'json_model',side_effect=[proposal,review])as vision,patch.object(engine,'paid_image',return_value=native),\
         patch('image_fidelity.selection',side_effect=[(Image.new('L',(100,100),255),{}),ValueError('衣料边界与独立衣料证据仍冲突，不能自动换色')]),\
         patch.object(engine,'automatic_image_correction'),patch('image_fidelity.calibrate')as grade:
     engine.process(task['id'])
    grade.assert_not_called();self.assertEqual(vision.call_count,2)
    self.assertEqual(task['results'][0]['image'],self.source)
    self.assertFalse(task['results'][0]['fidelity']['qualityApproved'])
    self.assertEqual(task['status'],'needs_revision'if blocking else'completed')

if __name__=='__main__':unittest.main()

import unittest,copy,hashlib
from generated_colour_variant import freeze,compile_prompt,references,inspect,REQUIRED

class ColourVariantContract(unittest.TestCase):
    def setUp(self):
        self.template={'id':'template-id','url':'template-url','sha256':'template-sha','style':'C50',
          'prompt':'Full original template hierarchy and photographs.','promptSha256':hashlib.sha256(b'Full original template hierarchy and photographs.').hexdigest()}
        self.parent={'id':'parent','status':'completed','productId':'product','results':[{'index':0,'image':'original',
          'review':{'blocking':False,'identityPreserved':True},'shot':{'apparelKind':'seller','copy':['PALE PINK JACKET','Plain stitched cuff'],'officialTemplate':self.template}}]}
        self.recipe={'mode':'repair','references':['source','target'],'referenceSha256':['source-sha','target-sha'],'brief':'Only requested colour variant','editTask':'free',
                     'sourceVariant':{'color':'Off White','children':[{'sku':'WHITE-S','size':'S'}]}}
        self.config={'sourceTaskId':'parent','sourceResultIndex':0,'targetColour':'Off White'}
    def frozen(self):
        return freeze(self.config,self.recipe,'product',lambda _:self.parent,lambda _: (None,'source-sha'),lambda _:copy.deepcopy(self.template))
    def checked(self,**overrides):
        result={k:True for k in REQUIRED+('templateFollowed',)}
        result.update(blocking=False,issues=[],warnings=[]);result.update(overrides);return result
    def test_reference_role_and_exact_colour_copy(self):
        self.recipe['generatedColourVariant']=self.frozen()
        self.assertEqual(references(self.recipe),['source','target','template-url'])
        self.assertEqual(self.recipe['generatedColourVariant']['expectedCopy'],['OFF WHITE JACKET','Plain stitched cuff'])
        self.assertFalse(self.recipe['generatedColourVariant']['preservesOutsidePixels'])
        self.assertIn(self.template['prompt'],compile_prompt(self.recipe))
    def test_unknown_source_not_resubmitted(self):
        for status in ('submission_unknown','interrupted','needs_revision','generating'):
            self.parent['status']=status
            with self.assertRaises(ValueError):self.frozen()
    def test_account_product_boundary(self):
        self.parent['productId']='another-product'
        with self.assertRaises(ValueError):self.frozen()
    def test_target_colour_must_match_frozen_variant(self):
        self.recipe['sourceVariant']['color']='Black'
        with self.assertRaisesRegex(ValueError,'真实颜色'):
            self.frozen()
    def test_changed_source_bytes(self):
        self.recipe['referenceSha256'][0]='changed'
        with self.assertRaises(ValueError):self.frozen()
    def test_unreviewed_master_blocked(self):
        del self.parent['results'][0]['review']['identityPreserved']
        with self.assertRaises(ValueError):self.frozen()
    def test_size_chart_shared_without_generation(self):
        self.parent['results'][0]['shot']['apparelKind']='size'
        with self.assertRaises(ValueError):self.frozen()
    def test_changed_template_not_silently_used(self):
        self.parent['results'][0]['shot']['officialTemplate']=dict(self.template,sha256='old-asset')
        with self.assertRaises(ValueError):self.frozen()
    def test_pixel_preservation_cannot_be_claimed(self):
        self.recipe['preserveSourcePixels']=True
        with self.assertRaises(ValueError):self.frozen()
    def test_original_quality_flags_cannot_be_waived(self):
        self.recipe['generatedColourVariant']=self.frozen()
        for key in REQUIRED+('templateFollowed',):
            result=inspect(self.recipe,{'image':'output'},'source',lambda *_:self.checked(**{key:False}))
            self.assertTrue(result['blocking'],key)
            self.assertTrue(result['issues'],key)
    def test_missing_review_flag_blocks(self):
        self.recipe['generatedColourVariant']=self.frozen()
        r=self.checked();del r['targetColourMatched']
        with self.assertRaises(ValueError):inspect(self.recipe,{'image':'output'},'source',lambda *_:r)
    def test_issues_block_even_when_all_flags_true(self):
        self.recipe['generatedColourVariant']=self.frozen()
        self.assertTrue(inspect(self.recipe,{'image':'output'},'source',lambda *_:self.checked(issues=['visible missed inset']))['blocking'])
    def test_actual_reference_and_template_reach_independent_review(self):
        self.recipe['generatedColourVariant']=self.frozen();calls=[]
        def model(instruction,context,images,scope):
            calls.append((context,images,scope));return self.checked()
        result=inspect(self.recipe,{'image':'output'},'source',model)
        self.assertFalse(result['blocking']);self.assertEqual(calls[0][1],['source','output','target','template-url'])
        self.assertEqual(calls[0][0]['targetReferenceSha256'],'target-sha')
    def test_white_requires_white_background_check(self):
        shot=self.parent['results'][0]['shot'];shot.update(apparelKind='white',copy=[],officialTemplate=None)
        self.recipe['generatedColourVariant']=self.frozen()
        with self.assertRaises(ValueError):inspect(self.recipe,{'image':'output'},'source',lambda *_:self.checked())
        self.assertFalse(inspect(self.recipe,{'image':'output'},'source',lambda *_:self.checked(whiteBackgroundCorrect=True))['blocking'])

class VariantEngineIntegration(unittest.TestCase):
    def setUp(self):
        import tempfile,uuid
        from pathlib import Path
        from unittest.mock import patch
        from PIL import Image
        import image_engine as e,app_paths
        self.e=e;self.patch=patch;self.uuid=uuid;self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for mod,key,value in ((app_paths,'DATA',Path(self.tmp.name)),(e,'DB',Path(self.tmp.name)/'test.sqlite3')):
            p=patch.object(mod,key,value);p.start();self.addCleanup(p.stop)
        self.source=e.cache(Image.new('RGB',(80,80),'pink'))['url']
        self.target=e.cache(Image.new('RGB',(80,80),'black'))['url']
        self.art=e.cache(Image.new('RGB',(80,80),'white'))['url']
        text='Original template prompt'
        self.template={'id':'123','url':self.art,'sha256':e.asset(self.art)[1],'style':'C50','prompt':text,'promptSha256':hashlib.sha256(text.encode()).hexdigest()}
        self.parent={'id':uuid.uuid4().hex,'requestId':uuid.uuid4().hex,'signature':'original','productId':'p','status':'completed',
          'results':[{'index':0,'image':self.source,'review':{'blocking':False,'identityPreserved':True},'shot':{'apparelKind':'seller','officialTemplate':self.template,'copy':['PALE PINK JACKET']}}]}
        e.save(self.parent)
    def start(self):
        import official_image_templates as lib
        data={'mode':'repair','requestId':self.uuid.uuid4().hex,'references':[self.source,self.target],'brief':'Exact Black colour variant','noText':False,
          'sourceVariant':{'receiptId':'a'*64},
          'generatedColourVariant':{'sourceTaskId':self.parent['id'],'sourceResultIndex':0,'targetColour':'Black'}}
        with self.patch.object(self.e,'require_codex'),self.patch.object(self.e,'launch'),self.patch.object(lib,'bind',return_value=self.template),self.patch('provider.text_settings',return_value={}),self.patch('image_source_variants.freeze',return_value={'color':'Black','children':[{'sku':'BLACK-S','size':'S'}]}):
            return self.e.start(data,{'id':'p','name':'jacket'})
    def verdict(self,passed=True):
        return {**{k:True for k in REQUIRED+('templateFollowed',)},'blocking':not passed,'issues':[]if passed else['missed inset'],'warnings':[],
          'allGarmentInstancesRecoloured':passed}
    def test_actual_repair_process_submits_three_roles_and_checks_four_images(self):
        task=self.start();calls=[]
        def paid(task,prompt,refs,**kw):
            calls.append((prompt,refs));return {'index':0,'image':self.target,'raw':self.target,'aspectValid':True,'providerJobId':'isolated-test'}
        with self.patch.object(self.e,'paid_image',side_effect=paid),self.patch.object(self.e,'json_model',return_value=self.verdict())as model:
            self.e.process(task['id'])
        result=self.e.get(task['id'])
        self.assertEqual(result['status'],'completed',result.get('error'))
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][1],[self.source,self.target,self.art])
        self.assertEqual(model.call_args.args[2],[self.source,self.target,self.target,self.art])
        self.assertEqual(result['results'][0]['productionMethod'],'codex-generated-colour-variant')
        self.assertEqual(result['request']['generatedColourVariant']['expectedCopy'],['BLACK JACKET'])
    def test_failed_full_review_keeps_original_one_correction_limit(self):
        task=self.start()
        def paid(task,prompt,refs,**kw):return {'index':0,'image':self.target,'raw':self.target,'aspectValid':True,'providerJobId':'isolated-'+str(len(task.get('attempts',[])))}
        with self.patch.object(self.e,'paid_image',side_effect=paid)as submit,self.patch.object(self.e,'json_model',side_effect=lambda *args:self.verdict(False)):
            self.e.process(task['id'])
        result=self.e.get(task['id'])
        self.assertEqual(result['status'],'needs_revision',result.get('error'))
        self.assertEqual(submit.call_count,2);self.assertEqual(len(result['automaticCorrections']),1)
        self.assertEqual(len(result['attempts']),1)
        self.assertIsNone(result['skuBinding']);self.assertFalse(result['humanReviewed'])

if __name__=='__main__':unittest.main()

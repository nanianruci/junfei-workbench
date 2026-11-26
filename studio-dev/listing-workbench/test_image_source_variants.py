import atexit,copy,hashlib,io,json,os,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image
_import=tempfile.TemporaryDirectory(prefix='source-variant-import-');atexit.register(_import.cleanup)
os.environ['JUNFEI_ACCOUNT_DATA']=_import.name
import app_paths,image_engine as engine,image_source_variants as variants

class SourceVariants(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  p=patch.object(app_paths,'DATA',self.root);p.start();self.addCleanup(p.stop)
  p=patch.object(engine,'DB',self.root/'image_engine.sqlite3');p.start();self.addCleanup(p.stop)
  self.url=self.image('black')
  self.product={'id':'p','sourceImport':{'market':'US','amazon':{'variants':[
   {'asin':'B000000001','sku':'black-s','color_name':'Black','size_name':'S'},
   {'asin':'B000000002','sku':'black-m','color_name':'Black','size_name':'M'}]}},
   'supplierColorReferences':[{'url':self.url,'color':'Black'}]}
 def image(self,color):
  folder=self.root/'uploads';folder.mkdir(exist_ok=True);name=uuid.uuid4().hex+'.png'
  if isinstance(color,Image.Image):color.save(folder/name)
  else:Image.new('RGB',(20,16),color).save(folder/name)
  return '/uploads/'+name
 def prepare(self):return variants.prepare(self.product,{'productId':'p','colorKey':'black','reference':self.url})
 def test_catalog_lists_actual_children_and_is_read_only(self):
  before=list(self.root.rglob('*'));data=variants.catalog(self.product)
  self.assertEqual(data['colors'][0]['color'],'Black');self.assertEqual(len(data['colors'][0]['children']),2)
  self.assertEqual(list(self.root.rglob('*')),before)
 def test_select_local_reference_never_downloads_or_launches(self):
  with patch('image_provider.remote_reference',side_effect=AssertionError('No fetch')),patch.object(engine,'launch',side_effect=AssertionError('No launch')):
   result=self.prepare();again=self.prepare()
  self.assertEqual(result,again);self.assertEqual(result['source']['url'],self.url)
  proof=variants.freeze(self.product,{'receiptId':result['association']['receiptId']},self.url,engine.asset)
  self.assertIsNone(proof['skuBinding']);self.assertEqual(proof['children'][0]['sku'],'black-s')
 def test_unlinked_or_excluded_reference_rejected_before_download(self):
  with patch('image_provider.remote_reference',side_effect=AssertionError('No fetch')):
   with self.assertRaises(ValueError):variants.prepare(self.product,{'colorKey':'black','reference':'https://m.media-amazon.com/images/I/unrelated.jpg'})
   self.product['sourceImport']['amazon']['listingEligibleVariants']=[]
   with self.assertRaises(ValueError):self.prepare()
 def test_remote_reference_is_cached_once_without_model_use(self):
  self.url='https://m.media-amazon.com/images/I/test.jpg';self.product['supplierColorReferences'][0]['url']=self.url
  raw=io.BytesIO();Image.new('RGB',(20,16),'black').save(raw,'JPEG')
  with patch('image_provider.remote_reference',return_value=(raw.getvalue(),'image/jpeg'))as fetch,patch.object(engine,'cache',side_effect=lambda image:{'url':self.image(image)}),patch.object(engine,'launch',side_effect=AssertionError('No models')):
   self.assertIsNone(variants.reviewed_reference_sha(self.product,'black',self.url));fetch.assert_not_called()
   result=self.prepare();self.assertEqual(result,self.prepare());self.assertEqual(fetch.call_count,1)
   self.assertEqual(variants.reviewed_reference_sha(self.product,'black',self.url),result['source']['sha256'])
   fetch.assert_called_once()
  self.assertTrue(result['source']['url'].startswith('/uploads/'))
 def test_tampered_or_other_source_cannot_claim_colour_association(self):
  receipt=self.prepare()['association']['receiptId']
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.image('red'),engine.asset)
  Image.new('RGB',(20,16),'white').save(self.root/self.url.lstrip('/'))
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset)
  with self.assertRaises(ValueError):variants.reviewed_reference_sha(self.product,'black',self.url)
 def test_product_sku_or_account_changes_invalidate_association(self):
  receipt=self.prepare()['association']['receiptId'];other=copy.deepcopy(self.product);other['id']='other'
  with self.assertRaises(ValueError):variants.freeze(other,{'receiptId':receipt},self.url,engine.asset)
  self.product['sourceImport']['amazon']['variants'][0]['sku']='changed-sku'
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset)
  with patch.object(app_paths,'DATA',self.root/'another-account'):
   with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset)
 def test_only_lossless_normalization_can_use_another_processing_file(self):
  receipt=self.prepare()['association']['receiptId'];normalized=self.image(Image.new('RGBA',(20,16),'black'))
  variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset,normalized_source=normalized)
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset,normalized_source=self.image('white'))
 def test_metadata_corruption_and_client_forged_fields_rejected(self):
  receipt=self.prepare()['association']['receiptId']
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt,'color':'White'},self.url,engine.asset)
  path=variants.folder()/(receipt+'.json');row=json.loads(path.read_text('utf-8'));row['color']='White';path.write_text(json.dumps(row),'utf-8')
  with self.assertRaises(ValueError):variants.freeze(self.product,{'receiptId':receipt},self.url,engine.asset)
 def test_submission_freezes_selected_colour_without_binding_or_model_launch(self):
  receipt=self.prepare()['association']['receiptId']
  data={'requestId':uuid.uuid4().hex,'mode':'repair','references':[self.url],'brief':'Preserve the garment.','editTask':'free','sourceVariant':{'receiptId':receipt}}
  with patch.object(engine,'require_codex'),patch.object(engine,'launch')as launch:
   task=engine.start(data,self.product)
  self.assertEqual(task['request']['sourceVariant']['color'],'Black')
  self.assertEqual(task['request']['sourceVariant']['sourceSha256'],engine.asset(self.url)[1])
  self.assertIsNone(task['skuBinding']);self.assertFalse(task['humanReviewed']);launch.assert_called_once_with(task['id'])

if __name__=='__main__':unittest.main()

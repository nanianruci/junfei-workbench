import hashlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import image_engine_delivery as delivery


class EngineDeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target, value in ((delivery.image_engine, 'DB'), (delivery.image_provider, 'ROOT')):
            mock = patch.object(target, value, Path(tmp.name) / 'image_engine.sqlite3' if value == 'DB' else Path(tmp.name))
            mock.start()
            self.addCleanup(mock.stop)
        self.product = {'id': 'P', 'masterColor': 'blue'}
        self.groups = {color: {'ready': True, 'color': color, 'children': [{'sku': color + '-S', 'size': 'S'}]}
                       for color in ('blue', 'red')}
        self.chart = {'columns': ['Size', 'Chest'], 'rows': [{'size': 'S', 'Chest': '80'}],
                      'units': 'cm', 'sourceEvidence': {'sourceKind': 'supplier'}}
        for target, name, value in (
            (delivery, 'scope', (self.groups, 'blue')),
            (delivery.shared_size_chart, 'from_product', self.chart),
            (delivery.image_composition, 'require', None),
        ):
            mock = patch.object(target, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.sources = {}
        self.tasks = {}
        self.ids = {'blue': '1' * 32, 'red': '2' * 32}
        self.derived_id = '3' * 32
        upload = Path(tmp.name) / 'uploads'
        upload.mkdir()
        for ci, (key, task_id) in enumerate(self.ids.items()):
            source = self.sources[key] = {'receiptId': key, 'colorKey': key, 'color': key,
                                           'children': [{'sku': key+'-S', 'size': 'S'}]}
            rows = []
            for slot, role in enumerate(delivery.ROLES):
                filename = format(100 * ci + slot, '032x') + '.png'
                raw = io.BytesIO()
                Image.new('RGB', (1600, 1600), (0, slot * 20, ci * 60)).save(raw, format='PNG')
                (upload / filename).write_bytes(raw.getvalue())
                rows.append({'index': slot, 'image': '/uploads/' + filename,
                             'imageSha256': hashlib.sha256(raw.getvalue()).hexdigest(),
                             'shot': {'apparelKind': role}, 'aspectValid': True,
                             'review': {'blocking': False, 'identityPreserved': True, 'templateFollowed': True},
                             'templateExecution': {'artworkReferenceSubmitted': True,
                                                   'completeOriginalPromptSubmitted': True}})
            rows[-1].update(productionMethod='verified-size-data-typesetting',
                            sizeDataSha256=hashlib.sha256(json.dumps(self.chart, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            self.tasks[task_id] = {'id': task_id, 'productId': 'P', 'status': 'completed', 'results': rows,
                'lifestyleSetReview': {'blocking': False}, 'request': {'mode': 'suite', 'sourceVariant': source,
                    'templateExecutionPolicy': 'official-artwork-v1',
                    'references': [rows[0]['image']], 'apparelSuite': {'sizeChart': self.chart}}}
        def save_upload(color):
            raw = io.BytesIO()
            Image.new('RGB', (1600, 1600), color).save(raw, format='PNG')
            content = raw.getvalue()
            filename = hashlib.md5(content).hexdigest() + '.png'
            (upload / filename).write_bytes(content)
            return '/uploads/' + filename
        real_red = save_upload((220, 0, 0))
        derived_red = save_upload((200, 0, 0))
        self.save_upload = save_upload
        self.tasks[self.derived_id] = {
            'id': self.derived_id, 'productId': 'P', 'status': 'completed',
            'request': {'mode': 'repair', 'sourceVariant': self.sources['red'],
                        'references': [self.tasks[self.ids['red']]['results'][1]['image'], real_red],
                        'generatedColourVariant': {'sourceTaskId': self.ids['red'], 'sourceResultIndex': 1,
                                                   'targetColour': 'red', 'slotKind': 'seller',
                                                   'sourceSha256': self.tasks[self.ids['red']]['results'][1]['imageSha256']}},
            'results': [{'index': 0, 'image': derived_red, 'imageSha256': self.asset(derived_red)[1], 'aspectValid': True,
                         'productionMethod': 'codex-generated-colour-variant',
                         'review': {'blocking': False, 'identityPreserved': True,
                                    'sameProductVariant': True, 'targetColourMatched': True,
                                    'materialPreserved': True, 'allGarmentInstancesRecoloured': True,
                                    'protectedContentPreserved': True, 'sourceLayoutPreserved': True,
                                    'textCorrect': True}}]}
        for target, name, callback in (
            (delivery.image_engine, 'get', self.tasks.__getitem__),
            (delivery.image_engine, 'asset', self.asset),
            (delivery.image_engine, 'list_tasks', lambda product_id, **kwargs: list(self.tasks.values())),
            (delivery.image_source_variants, 'freeze', lambda p, ref, *_: self.sources[ref['receiptId']]),
            (delivery.generated_gallery, 'prepare_delivery', self.prepare_delivery),
        ):
            mock = patch.object(target, name, side_effect=callback)
            mock.start()
            self.addCleanup(mock.stop)

    def asset(self, url):
        raw = (delivery.image_provider.ROOT / url.lstrip('/')).read_bytes()
        with Image.open(io.BytesIO(raw)) as image:
            return image.copy(), hashlib.sha256(raw).hexdigest()

    @staticmethod
    def prepare_delivery(raw):
        return raw, {'mime':'image/png','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}

    def approve(self, **data):
        return delivery.approve(2, self.product, {'productId': 'P', 'taskIds': self.ids,
                                                   'confirmed': True, 'expectedRevision': 1, **data})

    def test_explicit_selection_and_exact_shared_size_image(self):
        self.assertIsNone(delivery.galleries(self.product))
        with self.assertRaises(ValueError):
            self.approve(confirmed=False)
        receipt = self.approve()
        self.assertEqual(receipt, self.approve())
        rows = delivery.galleries(self.product)
        self.assertEqual(rows['blue'][6], rows['red'][6])
        self.assertNotEqual(rows['blue'][0], rows['red'][0])
        self.assertFalse(rows['blue'][0][1]['humanReviewed'])

    def test_changed_bytes_and_wrong_product_or_colour_block(self):
        self.approve()
        result = self.tasks[self.ids['red']]['results'][2]
        path = delivery.image_provider.ROOT / result['image'].lstrip('/')
        other = self.tasks[self.ids['blue']]['results'][2]['image']
        path.write_bytes((delivery.image_provider.ROOT / other.lstrip('/')).read_bytes())
        with self.assertRaisesRegex(ValueError, '候选文件'):
            delivery.galleries(self.product)
        with self.assertRaisesRegex(ValueError, '母版'):
            self.approve(taskIds={'blue': self.ids['red'], 'red': self.ids['blue']})
        self.tasks[self.ids['blue']]['productId'] = 'different'
        with self.assertRaisesRegex(ValueError, '当前账号和商品'):
            self.approve()

    def test_quality_checked_single_colour_image_replaces_its_exact_slot(self):
        view = delivery.view(self.product, 1)
        candidate = next(row for row in view['groups'][1]['candidates'] if row['id'] == self.ids['red'])
        self.assertEqual(candidate['slotCandidates']['1'][0]['id'], self.derived_id)
        self.approve(taskIds={'blue': self.ids['blue'], 'red': {
            'suiteTaskId': self.ids['red'], 'slotTaskIds': {'1': self.derived_id}}})
        rows = delivery.galleries(self.product)
        self.assertEqual(rows['red'][1][1]['engineTaskId'], self.derived_id)
        self.assertEqual(rows['blue'][6], rows['red'][6])

    def test_failed_colour_check_cannot_be_selected_for_delivery(self):
        self.tasks[self.derived_id]['results'][0]['review']['targetColourMatched'] = False
        view = delivery.view(self.product, 1)
        candidate = next(row for row in view['groups'][1]['candidates'] if row['id'] == self.ids['red'])
        self.assertNotIn('1', candidate['slotCandidates'])
        with self.assertRaises(ValueError):
            self.approve(taskIds={'blue': self.ids['blue'], 'red': {
                'suiteTaskId': self.ids['red'], 'slotTaskIds': {'1': self.derived_id}}})

    def test_two_colours_from_one_master_require_all_six_child_images(self):
        master = self.tasks[self.ids['blue']]
        template = self.tasks.pop(self.derived_id)
        del self.tasks[self.ids['red']]
        slots = {}
        for slot, role in enumerate(delivery.ROLES[:-1]):
            child = deepcopy(template)
            task_id = format(slot + 100, '032x')
            image = self.save_upload((230, slot * 16, 0))
            child.update(id=task_id)
            child['request']['generatedColourVariant'].update(sourceTaskId=master['id'], sourceResultIndex=slot,
                sourceSha256=master['results'][slot]['imageSha256'], slotKind=role)
            child['results'][0].update(image=image, imageSha256=self.asset(image)[1])
            child['results'][0]['review']['whiteBackgroundCorrect'] = True
            self.tasks[task_id] = child
            slots[str(slot)] = task_id
        view = delivery.view(self.product, 1)
        self.assertTrue(view['groups'][1]['candidates'][0]['usesMaster'])
        selected = {'blue': master['id'], 'red': {'suiteTaskId': master['id'], 'slotTaskIds': slots}}
        missing = deepcopy(selected)
        missing['red']['slotTaskIds'].pop('5')
        with self.assertRaisesRegex(ValueError, '六个'):
            self.approve(taskIds=missing)
        receipt = self.approve(taskIds=selected)
        self.assertEqual(receipt['bindings']['red']['derivedTaskIds'], slots)
        rows = delivery.galleries(self.product)
        self.assertEqual(rows['red'][6], rows['blue'][6])
        self.assertEqual([row[1]['engineTaskId'] for row in rows['red'][:6]], list(slots.values()))

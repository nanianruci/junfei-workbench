import {test} from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright';
import {validateRequest,applyPatch} from './existing-draft.mjs';

test('属性修改必须有逐字段依据，不接受无效尺寸或任意属性',()=>{
  const base={draftId:'172528873',store:'测试店铺',erpSku:'test'};
  for (const patch of [{color:'白色(White)'},{widthInches:'0'},{widthInches:'13 cm'},{material:''},{brand:'test'}]) {
    assert.throws(()=>validateRequest({...base,patch}));
  }
  assert.throws(()=>validateRequest({...base,patch:{widthInches:'0'},evidence:{widthInches:'source'}}));
  validateRequest({...base,patch:{material:''},evidence:{material:'供应商参数和图片冲突，清空待核实'}});
});

test('ERP属性DOM模拟：选择、清空、尺寸单位及重复字段拒绝',async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    const page=await browser.newPage();
    await page.route('**/*',r=>r.abort());
    const item = (label, value, options = []) => '<div class="el-form-item"><label><span class="normal">'+label+
      '</span></label><div class="el-select"><input value="'+value+'">'+(options.length?
      '<ul class="el-select-dropdown" style="display:none">'+options.map(option=>'<li><span>'+option+'</span></li>').join('')+'</ul>':'')+'</div></div>';
    await page.setContent([
      item('Color','黑色(Black)',['白色(White)','黑色(Black)']),
      item('Material Type','棉(Cotton)',['乙烯-醋酸乙烯酯(Ethylene Vinyl Acetate)']),
      item('Width Unit','英寸(Inches)'),
      item('Width shorter horizontal edge','4.73'),
    ].join(''));
    await page.evaluate(()=>{
      for(const row of document.querySelectorAll('.el-form-item')) {
        const input=row.querySelector('input'),dropdown=row.querySelector('.el-select-dropdown');
        if(!dropdown)continue;
        input.addEventListener('click',()=>dropdown.style.display='block');
        for(const li of dropdown.querySelectorAll('li'))li.addEventListener('click',()=>{input.value=li.innerText.trim();dropdown.style.display='none';});
        const clear=document.createElement('i');clear.className='el-icon-circle-close';clear.textContent='clear';
        clear.addEventListener('click',()=>input.value='');row.querySelector('.el-select').append(clear);
      }
    });
    await applyPatch(page,{color:'白色(White)',material:'乙烯-醋酸乙烯酯(Ethylene Vinyl Acetate)',widthInches:'5.12'});
    const values=await page.locator('.el-form-item input').evaluateAll(xs=>xs.map(x=>x.value));
    assert.deepEqual(values,['白色(White)','乙烯-醋酸乙烯酯(Ethylene Vinyl Acetate)','英寸(Inches)','5.12']);
    await applyPatch(page,{material:''});
    assert.equal(await page.locator('.el-form-item').filter({hasText:'Material Type'}).locator('input').inputValue(),'');
    await page.locator('input').nth(2).fill('厘米(Centimeters)');
    await assert.rejects(applyPatch(page,{widthInches:'13'}),/单位/);
    assert.equal(await page.locator('input').nth(3).inputValue(),'5.12');
    await page.evaluate(()=>{const row=document.querySelector('.el-form-item');document.body.append(row.cloneNode(true));});
    await assert.rejects(applyPatch(page,{color:'黑色(Black)'}),/唯一/);
  } finally {await browser.close();}
});

function skillCatalogStatus(skill) {
  if (skill.mode === 'codex') return skill.installed ? ['本机已安装', 'success'] : ['需在 Codex 安装', 'warning'];
  return skill.mode === 'editable' ? ['可编辑规则', 'active'] : ['工作台内置', ''];
}

function skillCatalogRow(skill) {
  const [status, tone] = skillCatalogStatus(skill);
  const version = Number.isInteger(skill.version) ? 'v' + skill.version : '—';
  let action;
  if (skill.mode === 'editable') {
    action = '<button type="button" class="ds-button" data-skill-edit>编辑规则</button>';
  } else if (skill.mode === 'codex') {
    action = '<button type="button" class="ds-button" data-skill-copy>复制调用</button>';
  } else {
    const label = skill.route === 'agent' ? '打开任务中心' : '打开产品档案';
    action = `<a class="ds-button" href="${esc(workspaceRoutes[skill.route])}" data-skill-route="${esc(skill.route)}">${label}</a>`;
  }
  return `<tr role="row" data-skill-id="${esc(skill.id)}">
    <th scope="row" role="rowheader"><strong>${esc(skill.name)}</strong>
      ${skill.invocation ? `<code>${esc(skill.invocation)}</code>` : ''}
    </th>
    <td class="skill-details" role="cell"><div class="skill-io"><span class="ds-muted">输入</span><span>${esc(skill.input)}</span>
      <span class="ds-muted">产出</span><span>${esc(skill.output)}</span></div></td>
    <td class="skill-status" role="cell"><span class="ds-status" data-tone="${tone}">${status}</span></td>
    <td class="skill-version" role="cell" data-skill-version>${version}</td>
    <td class="skill-action" role="cell">${action}</td>
  </tr>`;
}

function showSkillCatalog(node, data) {
  const skills = new Map(data.groups.flatMap(group => group.skills.map(skill => [skill.id, skill])));
  if (!skills.size) {
    node.innerHTML = '<p class="ds-panel ds-feedback ds-muted" role="status">当前没有可显示的技能。</p>';
    return;
  }
  node.innerHTML = data.groups.filter(group => group.skills.length).map((group, i) => `
    <section class="ds-panel" aria-labelledby="skillsGroup${i}">
      <div class="ds-panel-heading"><h2 id="skillsGroup${i}">${esc(group.label)}</h2></div>
      <div class="ds-table-wrap" tabindex="0" role="region" aria-label="${esc(group.label)}技能表格">
        <table class="ds-table skill-table" role="table">
          <thead><tr><th scope="col">技能</th><th scope="col">输入与产出</th><th scope="col">状态</th>
            <th scope="col">版本</th><th scope="col">管理</th></tr></thead>
          <tbody role="rowgroup">${group.skills.map(skillCatalogRow).join('')}</tbody>
        </table>
      </div>
    </section>`).join('');
  node.querySelectorAll('[data-skill-edit]').forEach(button => {
    const skill = skills.get(button.closest('tr').dataset.skillId);
    button.onclick = () => openCreativeSkills(skill.editor, {
      rule: skill.rule,
      onSaved: version => {
        node.querySelectorAll('[data-skill-id]').forEach(row => {
          if (skills.get(row.dataset.skillId).editor === skill.editor) {
            row.querySelector('[data-skill-version]').textContent = 'v' + version;
          }
        });
      },
    });
  });
  node.querySelectorAll('[data-skill-copy]').forEach(button => {
    const skill = skills.get(button.closest('tr').dataset.skillId);
    button.onclick = async () => {
      try {
        await navigator.clipboard.writeText(skill.invocation);
        toast('已复制，可粘贴到 Codex 调用');
      } catch {
        toast('可选中技能名下方的调用语句手动复制');
      }
    };
  });
  node.querySelectorAll('[data-skill-route]').forEach(link => {
    link.onclick = event => workbenchLink(event, link.dataset.skillRoute);
  });
}

window.renderWorkbenchSkills = function () {
  const content = document.getElementById('content');
  content.innerHTML = `<section class="ds skills-center" aria-labelledby="skillsTitle">
    <header class="ds-header">
      <div class="wb-title-label"><h1 id="skillsTitle">技能</h1>${workbenchHelp('可查看技能的输入和产出，编辑当前账号的文案与图片规则，或复制 Codex 技能的调用语句。','技能')}</div>
      <a class="ds-button" href="/tasks" data-skill-history>查看任务记录</a>
    </header>
    <div class="skills-center" data-skill-content></div>
  </section>`;
  content.querySelector('[data-skill-history]').onclick = event => workbenchLink(event, 'agent');
  const node = content.querySelector('[data-skill-content]');
  async function load() {
    node.innerHTML = '<p class="ds-panel ds-feedback ds-muted" role="status">正在读取技能…</p>';
    try {
      const response = await fetch('/api/workbench/skills');
      if (response.status === 404) throw Error('当前实例尚未加载技能接口，请重启开发工作台。');
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '技能目录无法读取');
      if (node.isConnected) showSkillCatalog(node, data);
    } catch (error) {
      if (!node.isConnected) return;
      node.innerHTML = '<div class="ds-panel ds-feedback ds-actions" role="alert"><span></span><button type="button" class="ds-button">重试</button></div>';
      node.querySelector('span').textContent = error.message;
      node.querySelector('button').onclick = load;
    }
  }
  void load();
};

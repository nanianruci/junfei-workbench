(() => {
  let sequence = 0;
  let openTip = null;
  const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);

  // Used by the shell's HTML renderers. React renders the same controls in HelpTip.
  window.workbenchHelp = (text, label) => {
    if (!text) return '';
    const id = 'wb-help-' + (++sequence);
    return '<span class="wb-help"><button type="button" class="wb-help-trigger" data-help-button' +
      ' aria-label="' + escape(label ? '查看' + label + '说明' : '查看说明') + '"' +
      ' aria-expanded="false" aria-controls="' + id + '">?</button>' +
      '<span class="wb-help-popover" id="' + id + '" role="note" hidden>' + escape(text) + '</span></span>';
  };

  function position() {
    if (!openTip) return;
    const {button, panel} = openTip;
    if (!button.isConnected || !panel.isConnected) {
      close();
      return;
    }
    const viewport = document.documentElement;
    const anchor = button.getBoundingClientRect();
    const bounds = panel.getBoundingClientRect();
    const padding = 12;
    const gap = 8;
    if (anchor.bottom < 0 || anchor.top > viewport.clientHeight) {
      close();
      return;
    }
    const left = Math.max(padding, Math.min(anchor.left, viewport.clientWidth - padding - bounds.width));
    const above = anchor.top - gap - bounds.height;
    const below = anchor.bottom + gap;
    const top = below + bounds.height <= viewport.clientHeight - padding || above < padding ? below : above;
    panel.style.left = left + 'px';
    panel.style.top = Math.max(padding, Math.min(top, viewport.clientHeight - padding - bounds.height)) + 'px';
  }

  function close(restoreFocus = false) {
    if (!openTip) return;
    const {button, panel} = openTip;
    openTip = null;
    if (typeof panel.hidePopover === 'function' && panel.matches(':popover-open')) panel.hidePopover();
    panel.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    button.removeAttribute('aria-describedby');
    if (restoreFocus && button.isConnected) button.focus({preventScroll: true});
  }

  document.addEventListener('click', (event) => {
    if (!(event.target instanceof Element)) return;
    const button = event.target.closest('[data-help-button]');
    if (!button) {
      if (!openTip?.panel.contains(event.target)) close();
      return;
    }
    event.preventDefault();
    const sameButton = openTip?.button === button;
    close();
    if (sameButton) return;
    const panel = document.getElementById(button.getAttribute('aria-controls'));
    if (!panel) return;
    panel.hidden = false;
    if (typeof panel.showPopover === 'function') {
      panel.setAttribute('popover', 'manual');
      panel.showPopover();
    }
    button.setAttribute('aria-expanded', 'true');
    button.setAttribute('aria-describedby', panel.id);
    openTip = {button, panel};
    position();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && openTip) {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    }
  }, true);
  window.addEventListener('resize', position, {passive: true});
  window.addEventListener('scroll', position, {capture: true, passive: true});
})();

import {useId, type ReactNode} from 'react';

export default function HelpTip({children, label}: {children: ReactNode; label?: string}) {
  const id = useId();
  return <span className="wb-help">
    <button type="button" className="wb-help-trigger" data-help-button
      aria-label={label ? '查看' + label + '说明' : '查看说明'} aria-expanded="false" aria-controls={id}>?</button>
    <span className="wb-help-popover" id={id} role="note" hidden>{children}</span>
  </span>;
}

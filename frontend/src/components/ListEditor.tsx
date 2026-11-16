import { Plus, X } from 'lucide-react';
import { useId, useState } from 'react';

/** Editable list of strings (acceptance criteria, verify commands). */
export function ListEditor({
  label,
  items,
  onChange,
  placeholder,
  mono = false,
}: {
  label: string;
  items: string[];
  onChange: (items: string[]) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  const [draft, setDraft] = useState('');
  const id = useId();
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    onChange([...items, v]);
    setDraft('');
  };
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="label">
        {label}
      </label>
      {items.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label={label}>
          {items.map((it, i) => (
            <li key={`${i}-${it}`} className="flex items-center gap-2 rounded-pixel border border-line bg-sunken px-2 py-1">
              <span className={`min-w-0 flex-1 break-words text-[13px] ${mono ? 'font-mono' : ''}`}>{it}</span>
              <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove ${label} ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}>
                <X className="size-3.5" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <input
          id={id}
          className={`field ${mono ? 'font-mono' : ''}`}
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={add}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
        />
        <button type="button" className="btn" onClick={add} aria-label={`Add ${label}`}>
          <Plus className="size-3.5" aria-hidden />
          Add
        </button>
      </div>
    </div>
  );
}

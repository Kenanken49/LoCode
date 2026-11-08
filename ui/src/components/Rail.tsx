/* Thin left rail: the app's three destinations and nothing else.

   Home is the chat column; Integrations and Settings slide a panel over it
   (Panels.tsx). Everything that used to be a header button now lives there, so
   the header keeps only project context and the runtime readouts. */

import { setView, useApp } from '../store';
import type { View } from '../store';

const DESTINATIONS: { id: View; glyph: string; label: string }[] = [
  { id: 'home', glyph: '⌂', label: 'Home' },
  { id: 'integrations', glyph: '⧉', label: 'Integrations' },
  { id: 'settings', glyph: '⚙', label: 'Settings' }
];

export function Rail() {
  const view = useApp((s) => s.view);
  return (
    <nav id="rail" aria-label="Main">
      <span className="mark" title="LOCODE" aria-hidden="true">LC</span>
      {DESTINATIONS.map((d) => (
        <button key={d.id} className="rail-btn" type="button" data-view={d.id}
          aria-label={d.label} title={d.label}
          aria-current={view === d.id ? 'page' : undefined}
          onClick={() => setView(d.id)}>{d.glyph}</button>
      ))}
    </nav>
  );
}

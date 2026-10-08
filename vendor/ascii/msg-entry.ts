import { mount } from './src/mount.ts';
import * as galaxy from './src/pieces/galaxy.ts';
import * as flow from './src/pieces/flow-field.ts';
import * as aurora from './src/pieces/aurora.ts';

// Browser-only decoration. Never reads content, identities, sessions or the network.
const pieces = { galaxy, flow, aurora };
const controllers = new Set<() => void>();
function attach(surface: HTMLElement) {
  const canvas = surface.querySelector<HTMLCanvasElement>('canvas[data-ascii-canvas]');
  if (!canvas || !canvas.getContext('2d')) return;
  const board = surface.closest<HTMLElement>('#public-board');
  const image = surface.querySelector<HTMLImageElement>('img');
  const controls = surface.querySelector<HTMLElement>('[data-ascii-controls]');
  const pause = document.querySelector<HTMLElement>(board ? '#public-board [data-pause]' : '#pause');
  const toggle = document.querySelector<HTMLButtonElement>('[data-ascii-toggle]');
  const caption = surface.querySelector<HTMLElement>('[data-ascii-caption]');
  let name = board?.dataset.asciiDefault === 'true' ? 'galaxy' : board ? 'community' : 'flow';
  let stop: (() => void) | undefined;
  let gone = false;
  let elapsed = 0;
  let frame = pieces.galaxy.default();
  function play() {
    stop?.(); stop = undefined;
    if (gone || surface.hidden || name === 'community') return;
    const piece = pieces[name as keyof typeof pieces];
    let previous = 0;
    const proxy = {meta: piece.meta, default: () => (t: number, env: any) => {
      elapsed += Math.max(0, t - previous); previous = t;
      return frame(elapsed, env);
    }};
    try { stop = mount(canvas!, proxy, {fps: pause?.getAttribute('aria-pressed') === 'true' ? 0 : 16}); }
    catch { choose('community'); }
  }
  function choose(next: string) {
    if (next !== 'community' && !(next in pieces)) return;
    name = next; elapsed = 0;
    if (name !== 'community') frame = pieces[name as keyof typeof pieces].default();
    canvas!.hidden = name === 'community';
    if (image) image.hidden = name !== 'community';
    surface.dataset.asciiScene = name;
    surface.querySelectorAll<HTMLButtonElement>('[data-ascii-piece]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.asciiPiece === name));
    });
    if (caption) caption.hidden = false;
    if (caption) caption.textContent = name === 'community' ? 'Shared artwork / 公共作品' : 'Character study / 字符习作 · ascii.rest';
    play();
  }
  controls?.addEventListener('click', event => {
    const button = (event.target as Element).closest<HTMLButtonElement>('[data-ascii-piece]');
    if (button) choose(button.dataset.asciiPiece!);
  });
  if (!board) {
    toggle?.addEventListener('click', () => {
      surface.hidden = !surface.hidden;
      toggle.setAttribute('aria-expanded', String(!surface.hidden));
      play();
    });
    surface.querySelector('[data-ascii-close]')?.addEventListener('click', () => {
      surface.hidden = true; toggle?.setAttribute('aria-expanded', 'false'); play(); toggle?.focus();
    });
  }
  board?.addEventListener('msg:board-updated', (event: Event) => {
    if ((event as CustomEvent).detail.svgChanged) choose('community');
  });
  if (pause) new MutationObserver(play).observe(pause, {attributes: true, attributeFilter: ['aria-pressed']});
  surface.dataset.asciiReady = 'true';
  if (controls) controls.hidden = false;
  choose(name);
  controllers.add(() => { gone = true; stop?.(); });
  addEventListener('pageshow', () => { gone = false; play(); });
}
function init() { document.querySelectorAll<HTMLElement>('[data-ascii-surface]').forEach(attach); }
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
else init();
addEventListener('pagehide', () => controllers.forEach(stop => stop()));

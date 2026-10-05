({ textMin = 11, hitMin = 44 } = {}) => {
  const findings = { font: [], contrast: [], target: [], overflow: [] };
  const excluded = 'script,style,noscript,svg,.sr-only,[aria-hidden="true"]';
  const selector = element => {
    if (element.id) return '#' + CSS.escape(element.id);
    const parts = [];
    for (let node = element; node && node !== document.body; node = node.parentElement) {
      let part = node.localName;
      if (node.classList.length) part += [...node.classList].map(name => '.' + CSS.escape(name)).join('');
      const peers = node.parentElement ? [...node.parentElement.children].filter(peer => peer.localName === node.localName) : [];
      if (peers.length > 1) part += ':nth-of-type(' + (peers.indexOf(node) + 1) + ')';
      parts.unshift(part);
      if (node.parentElement?.id) {
        parts.unshift('#' + CSS.escape(node.parentElement.id));
        break;
      }
    }
    return parts.join(' > ') || 'body';
  };
  const visible = element => {
    if (!element.getClientRects().length) return false;
    if (getComputedStyle(element).visibility !== 'visible') return false;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (node.hidden || style.display === 'none' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const clipRectangle = (element, rectangle) => {
    let left = rectangle.left, right = rectangle.right, top = rectangle.top, bottom = rectangle.bottom;
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node), box = node.getBoundingClientRect();
      if (['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX)) {
        left = Math.max(left, box.left); right = Math.min(right, box.right);
      }
      if (['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY)) {
        top = Math.max(top, box.top); bottom = Math.min(bottom, box.bottom);
      }
    }
    return { left, right, top, bottom, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
  };
  const clipped = (element, rectangles) => rectangles.some(rectangle => {
    const region = clipRectangle(element, rectangle);
    return region.width > 0 && region.height > 0;
  });
  const colorCanvas = document.createElement('canvas');
  colorCanvas.width = colorCanvas.height = 1;
  const colorContext = colorCanvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true });
  const rgba = value => {
    const match = /^rgba?\(([^)]+)\)$/.exec(value);
    if (match) {
      const parts = match[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      if (parts.every(Number.isFinite)) return [...parts.slice(0, 3), parts.length === 4 ? parts[3] : 1];
    }
    if (!colorContext || !CSS.supports('color', value)) throw new Error('无法审计的颜色：' + value);
    // computed style 可返回 Lab/P3；浏览器负责将它转换到 WCAG 使用的 sRGB。
    colorContext.clearRect(0, 0, 1, 1);
    colorContext.fillStyle = value;
    colorContext.fillRect(0, 0, 1, 1);
    const pixel = colorContext.getImageData(0, 0, 1, 1).data;
    return [pixel[0], pixel[1], pixel[2], pixel[3] / 255];
  };
  const over = (front, back) => {
    const alpha = front[3] + back[3] * (1 - front[3]);
    return alpha ? [0, 1, 2].map(i => (front[i] * front[3] + back[i] * back[3] * (1 - front[3])) / alpha).concat(alpha) : [0, 0, 0, 0];
  };
  const colors = (element, foreground) => {
    let front = foreground, back = [0, 0, 0, 0];
    for (let node = element; node; node = node.parentElement) {
      const style = getComputedStyle(node), layer = rgba(style.backgroundColor);
      if (layer) { front = over(front, layer); back = over(back, layer); }
      // opacity 作用于整个元素组，不能只检查未经合成的 color。
      const opacity = Number(style.opacity);
      front[3] *= opacity; back[3] *= opacity;
    }
    // 浏览器画布的缺省背景是白色；透明 body 仍要追溯到 html。
    return [over(front, [255, 255, 255, 1]), over(back, [255, 255, 255, 1])];
  };
  const luminance = color => color.slice(0, 3).map(channel => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
  const contrast = (front, back) => {
    const values = [luminance(over(front, back)), luminance(back)].sort((a, b) => a - b);
    return (values[1] + 0.05) / (values[0] + 0.05);
  };
  const inspectText = (element, text, style = getComputedStyle(element), pseudo = false) => {
    const size = parseFloat(style.fontSize);
    const info = { selector: selector(element), text: text.trim().replace(/\s+/g, ' ').slice(0, 100) };
    if (size < textMin) findings.font.push({ ...info, actual: size, minimum: textMin });
    const color = rgba(style.color);
    if (!color) return;
    if (pseudo) color[3] *= Number(style.opacity);
    const [front, back] = colors(element, color);
    const ratio = contrast(front, back);
    const minimum = size >= 24 || (size >= 18.666666 && Number(style.fontWeight) >= 700) ? 3 : 4.5;
    if (ratio + 0.001 < minimum) findings.contrast.push({ ...info, actual: Number(ratio.toFixed(3)), minimum, foreground: style.color, background: back });
  };
  const checked = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let text = walker.nextNode(); text; text = walker.nextNode()) {
    const element = text.parentElement;
    if (!text.textContent.trim() || !element || checked.has(element) || element.closest(excluded) || !visible(element)) continue;
    const range = document.createRange(); range.selectNodeContents(text);
    if (!clipped(element, [...range.getClientRects()])) continue;
    checked.add(element);
    inspectText(element, element.textContent);
  }
  for (const element of document.querySelectorAll('input,textarea,select')) {
    if (checked.has(element) || element.closest(excluded) || !visible(element)) continue;
    if (element.localName === 'input' && ['checkbox', 'radio', 'range', 'color', 'image'].includes(element.type)) continue;
    const text = element.localName === 'select' ? element.selectedOptions[0]?.textContent : element.type === 'password' && element.value ? '[masked password]' : element.type === 'file' ? element.files[0]?.name : element.value;
    if (text?.trim()) inspectText(element, text);
    else if (element.placeholder?.trim()) inspectText(element, element.placeholder, getComputedStyle(element, '::placeholder'), true);
  }
  const inlineProse = element => {
    if (element.localName !== 'a' || getComputedStyle(element).display !== 'inline') return false;
    if (element.closest('nav,header,footer,.post-meta,.document-outline,.post-title,.search-results')) return false;
    const prose = element.closest('.prose'), parent = element.closest('p,li,blockquote');
    if (!prose || !parent || !prose.contains(parent) || element.closest('h1,h2,h3,h4,h5,h6')) return false;
    const remainder = parent.cloneNode(true);
    remainder.querySelectorAll('a,time').forEach(node => node.remove());
    if (parent.localName === 'li') return [...remainder.childNodes].some(node => node.nodeType === Node.TEXT_NODE && /[\p{L}\p{N}]/u.test(node.textContent));
    return Boolean(remainder.textContent.trim());
  };
  for (const element of document.querySelectorAll('a[href],button,input:not([type="hidden"]),select,summary,textarea')) {
    if (element.closest('.sr-only') || !visible(element) || inlineProse(element)) continue;
    const rectangle = clipRectangle(element, element.getBoundingClientRect());
    if (!clipped(element, [rectangle])) continue;
    const regions = [rectangle, ...[...(element.labels || [])].filter(visible).map(label => clipRectangle(label, label.getBoundingClientRect()))];
    if (regions.some(region => region.width + 0.01 >= hitMin && region.height + 0.01 >= hitMin)) continue;
    findings.target.push({ selector: selector(element), text: (element.textContent || element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.localName).trim().replace(/\s+/g, ' ').slice(0, 100), width: Number(rectangle.width.toFixed(2)), height: Number(rectangle.height.toFixed(2)), minimum: hitMin });
  }
  const overflow = Math.max(0, document.documentElement.scrollWidth - innerWidth);
  if (overflow) findings.overflow.push({ selector: 'html', text: 'document horizontal overflow', actual: overflow, maximum: 0 });
  return findings;
}

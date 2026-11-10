/**
 * Minimal DOM helpers. Text is only ever inserted as text nodes (never innerHTML),
 * so model output and tenant strings can't inject markup.
 */

type Attrs = Record<string, string | boolean | undefined>;
type Child = Node | string;

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  children: Child[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === false) continue;
    node.setAttribute(name, value === true ? '' : value);
  }
  node.append(...children); // strings become text nodes
  return node;
}

/**
 * Render "What are you *building*?" as text + <em>building</em>.
 * Splitting on "*" puts the starred words at odd indexes.
 */
export function withAccent(text: string): Child[] {
  return text.split('*').map((part, i) => (i % 2 === 1 ? el('em', {}, [part]) : part));
}

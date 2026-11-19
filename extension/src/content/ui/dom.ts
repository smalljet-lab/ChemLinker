/**
 * Tiny element builder. Text only ever goes in through text nodes; attribute
 * names are always literals in our code. No markup strings anywhere.
 */

export type Child = Node | string | number | null | undefined | false;

export interface Props {
  class?: string;
  text?: string;
  /** Attribute values are plain strings; names come from our own code only. */
  attrs?: Record<string, string>;
  /** Focus-restore key (`data-key`). */
  key?: string;
  on?: { [K in keyof HTMLElementEventMap]?: (event: HTMLElementEventMap[K]) => void };
}

export type El = <K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props?: Props | null,
  ...children: Child[]
) => HTMLElementTagNameMap[K];

export function makeEl(doc: Document): El {
  return function el(tag, props, ...children) {
    const node = doc.createElement(tag);
    if (props) {
      if (props.class) node.className = props.class;
      if (props.attrs) for (const [name, value] of Object.entries(props.attrs)) node.setAttribute(name, value);
      if (props.key) node.dataset.key = props.key;
      if (props.on) {
        for (const [type, handler] of Object.entries(props.on)) {
          if (handler) node.addEventListener(type, handler as EventListener);
        }
      }
      if (props.text !== undefined) node.textContent = props.text;
    }
    append(doc, node, children);
    return node;
  };
}

export function append(doc: Document, parent: Node, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    parent.appendChild(typeof child === "string" || typeof child === "number" ? doc.createTextNode(String(child)) : child);
  }
}

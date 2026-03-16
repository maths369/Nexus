import { Extension, type Range } from "@tiptap/core";
import Suggestion, { type SuggestionProps } from "@tiptap/suggestion";
import tippy, { type Instance as TippyInstance } from "tippy.js";

export type SlashItem = {
  title: string;
  description: string;
  icon?: string;
  category?: string;
  command: (props: { range: Range; editor: any }) => void;
};

type GroupedItems = {
  category: string;
  items: SlashItem[];
};

const CATEGORY_ORDER = ["基础", "格式", "列表", "表格"];

function groupItemsByCategory(items: SlashItem[]): GroupedItems[] {
  const groups: Record<string, SlashItem[]> = {};

  for (const item of items) {
    const cat = item.category || "基础";
    if (!groups[cat]) {
      groups[cat] = [];
    }
    groups[cat].push(item);
  }

  const sorted: GroupedItems[] = [];
  for (const cat of CATEGORY_ORDER) {
    if (groups[cat]) {
      sorted.push({ category: cat, items: groups[cat] });
      delete groups[cat];
    }
  }
  for (const [cat, items] of Object.entries(groups)) {
    sorted.push({ category: cat, items });
  }

  return sorted;
}

export function SlashCommand(items: SlashItem[]) {
  return Extension.create({
    name: "slash-command",
    addOptions() {
      return {
        suggestion: {
          char: "/",
          startOfLine: true,
          allowSpaces: false,
          command: ({ editor, range, props: item }: { editor: any; range: Range; props: any }) => {
            (item as SlashItem).command({ editor, range });
          },
        },
      };
    },
    addProseMirrorPlugins() {
      return [
        Suggestion({
          editor: this.editor,
          ...this.options.suggestion,
          items: ({ query }) => {
            if (!query) return items;
            return items.filter(
              (item) =>
                item.title.toLowerCase().includes(query.toLowerCase()) ||
                item.description.toLowerCase().includes(query.toLowerCase()),
            );
          },
          render: () => {
            let component: HTMLElement;
            let popup: TippyInstance | null = null;
            let selectedIndex = 0;
            let flatItems: SlashItem[] = [];

            function updateSelection(next: number, list: HTMLElement) {
              const buttons = list.querySelectorAll(".slash-item");
              const count = buttons.length;
              if (count === 0) return;
              selectedIndex = ((next % count) + count) % count;
              buttons.forEach((node, idx) => {
                (node as HTMLElement).classList.toggle("active", idx === selectedIndex);
              });
              (buttons[selectedIndex] as HTMLElement)?.scrollIntoView({ block: "nearest" });
            }

            return {
              onStart: (props) => {
                component = document.createElement("div");
                component.className = "slash-menu";

                const header = document.createElement("div");
                header.className = "slash-header";
                header.innerHTML = `<span class="slash-header-icon">/</span><span class="slash-header-text">\u8F93\u5165\u547D\u4EE4 \u6216 \u7B5B\u9009...</span>`;
                component.append(header);

                const list = document.createElement("div");
                list.className = "slash-list";
                component.append(list);

                flatItems = itemsToDOM(props, list);

                popup = tippy("body", {
                  getReferenceClientRect: props.clientRect as any,
                  appendTo: () => document.body,
                  content: component,
                  showOnCreate: true,
                  interactive: true,
                  trigger: "manual",
                  placement: "bottom-start",
                  theme: "light",
                  animation: "shift-away",
                  arrow: false,
                  maxWidth: 340,
                })[0];

                selectedIndex = 0;
                updateSelection(selectedIndex, list);
              },
              onUpdate: (props) => {
                const list = component.querySelector(".slash-list") as HTMLElement;
                flatItems = itemsToDOM(props, list);
                selectedIndex = 0;
                updateSelection(selectedIndex, list);
                popup?.setProps({ getReferenceClientRect: props.clientRect as any });
              },
              onKeyDown: (props) => {
                const list = component.querySelector(".slash-list") as HTMLElement;
                if (props.event.key === "ArrowDown") {
                  updateSelection(selectedIndex + 1, list);
                  return true;
                }
                if (props.event.key === "ArrowUp") {
                  updateSelection(selectedIndex - 1, list);
                  return true;
                }
                if (props.event.key === "Enter") {
                  const buttons = list.querySelectorAll(".slash-item");
                  const node = buttons[selectedIndex] as HTMLElement;
                  node?.click();
                  return true;
                }
                return false;
              },
              onExit: () => {
                popup?.destroy();
                popup = null;
              },
            };

            function itemsToDOM(props: SuggestionProps, list: HTMLElement): SlashItem[] {
              list.innerHTML = "";
              const available = (props.items as SlashItem[] | undefined) || [];
              const flat: SlashItem[] = [];

              const groups = groupItemsByCategory(available);

              if (groups.length === 0) {
                const empty = document.createElement("div");
                empty.className = "slash-empty";
                empty.textContent = "\u6CA1\u6709\u5339\u914D\u7684\u547D\u4EE4";
                list.append(empty);
                return flat;
              }

              groups.forEach((group) => {
                const headerEl = document.createElement("div");
                headerEl.className = "slash-group-header";
                headerEl.textContent = group.category;
                list.append(headerEl);

                group.items.forEach((item) => {
                  flat.push(item);
                  const el = document.createElement("button");
                  el.type = "button";
                  el.className = "slash-item";
                  el.innerHTML = `
                    <span class="icon">${item.icon || "\u2022"}</span>
                    <div class="meta">
                      <div class="title">${item.title}</div>
                      <div class="desc">${item.description}</div>
                    </div>
                  `;
                  el.addEventListener("click", () => props.command(item));
                  list.append(el);
                });
              });

              return flat;
            }
          },
        }),
      ];
    },
  });
}

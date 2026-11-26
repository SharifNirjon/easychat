/**
 * Shapes shared across the widget. They mirror the API: the public tenant config
 * (api/models.py TenantConfig) and the UI blocks (api/blocks.py). The server validates
 * blocks; the widget still renders defensively (text only, URLs re-checked).
 */

export type Role = 'user' | 'assistant';

export interface ChatMessage {
  role: Role;
  content: string;
  blocks?: Block[];
}

export interface HomeAction {
  icon: string;
  title: string;
  subtitle: string;
  message?: string | null;
  url?: string | null;
}

export interface WidgetConfig {
  company_name: string;
  bot_name: string;
  greeting: {
    title: string;
    headline: string; // words wrapped in *asterisks* render in the accent serif
    subtitle: string;
  };
  disclosure?: string | null;
  teaser: string;
  home: {
    promo?: HomeAction | null;
    cards: HomeAction[];
    chips_label: string;
    chips: string[];
  };
  privacy_url: string;
  theme: {
    primary: string;
    accent: string;
    surface: string;
    text: string;
  };
}

// --- UI blocks --------------------------------------------------------------------------

export type Action = { kind: 'send'; text: string } | { kind: 'link'; url: string };

/** Chat forms the server handles (api/blocks.py FormName). */
export type FormName = 'order_lookup' | 'order_code';

/** A submitted form. Client-side only, never part of a block; `label` is the visitor's bubble text. */
export interface FormSubmit {
  kind: 'form';
  form: FormName;
  values: Record<string, string>;
  label: string;
}
export type Tone = 'info' | 'success' | 'warning' | 'danger';

export interface ImageRef {
  url: string;
  alt?: string;
}

export interface Card {
  title: string;
  subtitle?: string;
  badge?: string;
  image?: ImageRef;
  rating?: number;
  reviews?: string;
  price?: string;
  action?: Action;
}

export interface Choice {
  id: string;
  label: string;
  detail?: string;
  image?: ImageRef;
}

export interface TimelineStep {
  label: string;
  detail?: string;
  state: 'done' | 'current' | 'upcoming';
}

export type FormField =
  | {
      kind: 'text' | 'email' | 'textarea' | 'code';
      name: string;
      label: string;
      placeholder?: string;
      required?: boolean;
      value?: string;
    }
  | { kind: 'checkbox'; name: string; label: string }
  | { kind: 'rating'; name: string; label: string }
  | { kind: 'tags'; name: string; label: string; options: string[] };

export type Block =
  | { type: 'chips'; items: { label: string; action: Action }[] }
  | { type: 'buttons'; items: { label: string; style?: 'primary' | 'secondary'; action: Action }[] }
  | { type: 'card'; card: Card }
  | { type: 'carousel'; items: Card[] }
  | {
      type: 'choice_list';
      mode: 'single' | 'multi';
      question: string;
      step?: { current: number; total: number };
      options: Choice[];
      submit_label?: string;
    }
  | {
      type: 'timeline';
      eyebrow?: string;
      title: string;
      status?: { label: string; tone?: Tone };
      steps: TimelineStep[];
      footer?: string;
      link?: { kind: 'link'; url: string };
    }
  | { type: 'form'; title?: string; fields: FormField[]; submit_label: string; submit?: FormName }
  | { type: 'sources'; items: { title: string; url: string }[] }
  | { type: 'notice'; tone?: Tone; text: string };

export type ActionHandler = (action: Action | FormSubmit) => void;

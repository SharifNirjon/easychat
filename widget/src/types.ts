/** Shapes shared across the widget. Mirrors the API's public tenant config. */

export type Role = 'user' | 'assistant';

export interface ChatMessage {
  role: Role;
  content: string;
}

export interface WidgetConfig {
  site_id: string;
  company_name: string;
  bot_name: string;
  greeting: {
    title: string;
    headline: string; // words wrapped in *asterisks* render in the accent serif
    subtitle: string;
  };
  disclosure: string;
  teaser: string;
  suggested_prompts: string[];
  privacy_url: string;
  theme: {
    primary: string;
    primary_dark: string;
    accent: string;
  };
}

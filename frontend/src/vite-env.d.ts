/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Optional absolute origin of the API (default: same origin as the UI). */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

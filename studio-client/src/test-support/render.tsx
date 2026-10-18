// App-local render entry point. The dashboard's component tree needs the same
// QueryClientProvider wrapper client/ uses, so we re-export client-core's
// `renderWithProviders` (also as `render`) rather than re-implementing it. When
// studio-client grows app-specific providers (a theme, a prefs context), wrap them
// here and keep call sites importing from this module.
export {
  renderWithProviders,
  renderWithProviders as render,
} from "client-core/test-support/render";

// In the app, `import "server-only"` makes the BUILD fail if the module is ever pulled into browser code.
// Under Vitest there is no such bundler condition (the real package throws), so tests alias it to this empty module.
export {};

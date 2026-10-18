// The radix Dialog renders its overlay as the content's immediately preceding sibling; a
// test presses on that element to simulate a pointer outside the panel.
export function backdropOf(dialog: HTMLElement): HTMLElement {
  const backdrop = dialog.previousElementSibling;
  if (!(backdrop instanceof HTMLElement)) throw new Error("backdrop missing");
  return backdrop;
}

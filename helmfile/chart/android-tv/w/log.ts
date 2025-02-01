export function log(...x: unknown[]) {
  console.log(new Date().toJSON(), ...x);
}

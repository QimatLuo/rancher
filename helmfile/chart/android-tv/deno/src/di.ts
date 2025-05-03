import { ObservableInput, ReplaySubject } from "rxjs";

export const CmdOutput = new ReplaySubject<
  (cmd: Deno.Command) => ObservableInput<Deno.CommandOutput>
>(1);

export const Log = new ReplaySubject<
  (...any: unknown[]) => void
>(1);

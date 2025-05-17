import { ObservableInput, ReplaySubject, Subject } from "rxjs";

export const CmdOutput = new ReplaySubject<
  (cmd: Deno.Command) => ObservableInput<Deno.CommandOutput>
>(1);

export const Log = new Subject<unknown>();

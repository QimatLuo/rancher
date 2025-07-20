const command = new Deno.Command("adb", {
  args: ["logcat"],
  stdin: "piped",
  stdout: "piped",
});
const process = command.spawn();
const reader = process.stdout.getReader();

function loop(cb: () => void) {
  return reader.read().then(({ done, value }) => {
    if (done) return;
    loop(cb);

    const isAd = new TextDecoder()
      .decode(value)
      .split("\n")
      .filter((x) => x.includes("youtube"))
      .filter((x) => x.includes("onPlaybackStateChanged"))
      .some((x) => x.includes("actions=51"));

    if (isAd) cb();
  });
}

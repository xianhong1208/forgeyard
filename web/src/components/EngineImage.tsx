// 1.5.2 (#189): the image (or program) an engine runs, next to the engine's name: the first one by its tag, "+N" for
// the others, every full reference in the tooltip. One line, mono and muted. 1.6.2: when the space is short the
// reference is cut once, at its start ("…stable-diffusion.cpp:master-cuda"), so the tag stays readable and the "+N"
// is never cut.
import { engineRunsTitle, type EngineRun } from "../lib/engines";

export function EngineImage({ runs, className }: { runs: EngineRun[]; className?: string }) {
  if (!runs.length) return null;
  return (
    <span className={`engine-image mono muted${className ? ` ${className}` : ""}`} title={engineRunsTitle(runs)} data-testid="engine-image">
      <span className="engine-image-name"><bdi dir="ltr">{runs[0].label}</bdi></span>
      {runs.length > 1 && <span className="engine-image-more">+{runs.length - 1}</span>}
    </span>
  );
}

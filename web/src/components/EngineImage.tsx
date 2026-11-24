// 1.5.2 (#189): the image (or program) an engine runs, next to the engine's name: the first one by its tag, "+N" for
// the others, every full reference in the tooltip. One line, mono and muted; when the space is short (a phone, a
// narrow card) the repository is cut with an ellipsis before the tag is, and the "+N" never is.
import { engineRunsTitle, splitImage, type EngineRun } from "../lib/engines";

export function EngineImage({ runs, className }: { runs: EngineRun[]; className?: string }) {
  if (!runs.length) return null;
  const [repo, tag] = splitImage(runs[0].label);
  return (
    <span className={`engine-image mono muted${className ? ` ${className}` : ""}`} title={engineRunsTitle(runs)} data-testid="engine-image">
      <span className="engine-image-name"><span className="engine-image-repo">{repo}</span>{tag && <span className="engine-image-tag">{tag}</span>}</span>
      {runs.length > 1 && <span className="engine-image-more">+{runs.length - 1}</span>}
    </span>
  );
}

export function Thumb({ src, alt = "", width = 96 }: { src: string | null; alt?: string; width?: number }) {
  const style = { width, height: Math.round((width * 9) / 16) };
  if (!src) return <span className="thumb thumb-empty" style={style} aria-hidden="true" />;
  return <img className="thumb" src={src} alt={alt} loading="lazy" decoding="async" style={style} />;
}

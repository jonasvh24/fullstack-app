export function ErrorText({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return (
    <p className="err-text" role="alert">
      {error}
    </p>
  );
}

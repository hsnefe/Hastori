// Numbers typed into a form. `Number("")` is 0 and `Number("  ")` is 0: an emptied field must not
// become a zero threshold. Turkish keyboards type a comma for the decimal point; a thousands
// separator ("1.000") is refused rather than read as 1.

const DECIMAL = /^-?\d+([.,]\d+)?$/;
const INTEGER = /^\d+$/;
// "1.000" and "1,234" are a thousands separator to one reader and a decimal to another: refuse the
// shape instead of guessing (write 1000, or 1.0 / 1,234 with a different count of decimals).
const AMBIGUOUS = /^-?[1-9]\d{0,2}[.,]\d{3}$/;

/** A decimal number, or null for anything else (empty, text, "1.000", "1.234,5"). */
export function parseDecimal(text: string): number | null {
  const t = text.trim();
  if (!DECIMAL.test(t) || AMBIGUOUS.test(t)) return null;
  const n = Number(t.replace(",", "."));
  return Number.isFinite(n) ? n : null;
}

/** A whole number >= 0, or null. */
export function parseInteger(text: string): number | null {
  const t = text.trim();
  if (!INTEGER.test(t)) return null;
  const n = Number(t);
  return Number.isSafeInteger(n) ? n : null;
}

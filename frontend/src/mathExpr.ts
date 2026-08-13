// Safe arithmetic expression evaluator for numeric param fields -- lets
// someone type "9/3" into an int/float field and have it settle to 3, without
// resorting to eval()/Function() on arbitrary user-typed text. Grammar:
//   expr   := term (('+'|'-') term)*
//   term   := factor (('*'|'/') factor)*
//   factor := ('+'|'-')* (number | '(' expr ')')
// No variables, no functions -- just the four operators and parens.

const NUMBER_RE = /^\d+(\.\d+)?/;

class ExprParser {
  private pos = 0;
  constructor(private readonly src: string) {}

  parse(): number | null {
    const value = this.parseExpr();
    if (value === null) return null;
    this.skipSpace();
    if (this.pos !== this.src.length) return null; // trailing garbage, e.g. "3)" or "3 4"
    return Number.isFinite(value) ? value : null;
  }

  private skipSpace() {
    while (this.pos < this.src.length && /\s/.test(this.src[this.pos])) this.pos++;
  }

  private parseExpr(): number | null {
    let value = this.parseTerm();
    if (value === null) return null;
    for (;;) {
      this.skipSpace();
      const op = this.src[this.pos];
      if (op !== "+" && op !== "-") break;
      this.pos++;
      const rhs = this.parseTerm();
      if (rhs === null) return null;
      value = op === "+" ? value + rhs : value - rhs;
    }
    return value;
  }

  private parseTerm(): number | null {
    let value = this.parseFactor();
    if (value === null) return null;
    for (;;) {
      this.skipSpace();
      const op = this.src[this.pos];
      if (op !== "*" && op !== "/") break;
      this.pos++;
      const rhs = this.parseFactor();
      if (rhs === null) return null;
      if (op === "/") {
        if (rhs === 0) return null; // division by zero is a rejected edit, never Infinity
        value = value / rhs;
      } else {
        value = value * rhs;
      }
    }
    return value;
  }

  private parseFactor(): number | null {
    this.skipSpace();
    if (this.src[this.pos] === "-") {
      this.pos++;
      const value = this.parseFactor();
      return value === null ? null : -value;
    }
    if (this.src[this.pos] === "+") {
      this.pos++;
      return this.parseFactor();
    }
    if (this.src[this.pos] === "(") {
      this.pos++;
      const value = this.parseExpr();
      if (value === null) return null;
      this.skipSpace();
      if (this.src[this.pos] !== ")") return null;
      this.pos++;
      return value;
    }
    this.skipSpace();
    const match = NUMBER_RE.exec(this.src.slice(this.pos));
    if (!match) return null;
    this.pos += match[0].length;
    return Number(match[0]);
  }
}

/**
 * Evaluates a small arithmetic expression typed into a numeric param field
 * (e.g. "9/3" -> 3, "2*(1+1)" -> 4). Returns null for anything that isn't a
 * clean, fully-consumed expression -- empty input, unbalanced parens, stray
 * characters, or division by zero. Callers must treat null as a rejected
 * edit, never coerce it to 0 or NaN.
 */
export function evaluateMathExpression(input: string): number | null {
  const trimmed = input.trim();
  if (trimmed === "") return null;
  return new ExprParser(trimmed).parse();
}

import { describe, expect, it } from 'vitest';
import { escapeCsvField, toCsv } from './csv';

describe('escapeCsvField', () => {
  it('leaves plain values unquoted', () => {
    expect(escapeCsvField('Ava')).toBe('Ava');
    expect(escapeCsvField(42)).toBe('42');
    expect(escapeCsvField(3.5)).toBe('3.5');
  });

  it('renders null and undefined as empty fields', () => {
    expect(escapeCsvField(null)).toBe('');
    expect(escapeCsvField(undefined)).toBe('');
  });

  it('quotes fields containing commas, quotes, CR or LF and doubles inner quotes', () => {
    expect(escapeCsvField('Smith, John')).toBe('"Smith, John"');
    expect(escapeCsvField('5" screen')).toBe('"5"" screen"');
    expect(escapeCsvField('line 1\nline 2')).toBe('"line 1\nline 2"');
    expect(escapeCsvField('a\r\nb')).toBe('"a\r\nb"');
    expect(escapeCsvField('"quoted"')).toBe('"""quoted"""');
  });

  it('quotes fields with leading or trailing whitespace so it survives parsing', () => {
    expect(escapeCsvField(' padded ')).toBe('" padded "');
  });
});

describe('escapeCsvField formula neutralization (CSV injection)', () => {
  it('prefixes text that a spreadsheet would evaluate as a formula', () => {
    expect(escapeCsvField('=HYPERLINK("http://x")')).toBe(`"'=HYPERLINK(""http://x"")"`);
    expect(escapeCsvField('+1+2')).toBe("'+1+2");
    expect(escapeCsvField('-2+3')).toBe("'-2+3");
    expect(escapeCsvField('@SUM(A1:A2)')).toBe("'@SUM(A1:A2)");
  });

  it('leaves numbers, including negative numbers, untouched', () => {
    expect(escapeCsvField(-42)).toBe('-42');
    expect(escapeCsvField(3.5)).toBe('3.5');
  });

  it('leaves ordinary text untouched', () => {
    expect(escapeCsvField('California')).toBe('California');
    expect(escapeCsvField('a=b')).toBe('a=b');
  });
});

describe('toCsv', () => {
  it('writes a header and rows separated by CRLF without a trailing line break', () => {
    const csv = toCsv(
      ['CustomerID', 'Name', 'State', 'Notes'],
      [
        [1, 'Ava', 'California', null],
        [2, 'O\'Brien, Liam', 'New York', 'Says "hi"'],
      ],
    );
    expect(csv).toBe(
      'CustomerID,Name,State,Notes\r\n1,Ava,California,\r\n2,"O\'Brien, Liam",New York,"Says ""hi"""',
    );
  });

  it('returns only the header for an empty result', () => {
    expect(toCsv(['a', 'b'], [])).toBe('a,b');
  });

  it('escapes header cells too and keeps unicode intact', () => {
    expect(toCsv(['Total, USD', 'Café'], [['€1,000', 'naïve']])).toBe('"Total, USD",Café\r\n"€1,000",naïve');
  });
});

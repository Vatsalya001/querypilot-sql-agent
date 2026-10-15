/** Example prompts for the empty state, grouped by capability. */

export type ExampleGroupId = 'generate' | 'follow_up' | 'optimize' | 'debug' | 'explain' | 'guardrails';

export interface ExamplePrompt {
  /** Text sent to the agent. */
  prompt: string;
  /** Optional second message, placed in the input after the first answer (conversation context demo). */
  followUp?: string;
}

export interface ExampleGroup {
  id: ExampleGroupId;
  title: string;
  description: string;
  examples: ExamplePrompt[];
}

export const EXAMPLE_GROUPS: readonly ExampleGroup[] = [
  {
    id: 'generate',
    title: 'Generate',
    description: 'Plain English to validated SQL',
    examples: [
      { prompt: 'Show all employees hired after January 2024' },
      { prompt: 'Top 5 products by total revenue in 2025' },
      { prompt: 'Average salary by department' },
    ],
  },
  {
    id: 'follow_up',
    title: 'Follow-up',
    description: 'Refine the previous query in context',
    examples: [{ prompt: 'Show all customers', followUp: 'Only those from California' }],
  },
  {
    id: 'optimize',
    title: 'Optimize',
    description: 'Readability, indexes and unnecessary joins',
    examples: [
      {
        prompt:
          "Optimize this query: SELECT * FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID JOIN Employees e ON o.EmployeeID = e.EmployeeID WHERE c.State = 'Texas'",
      },
    ],
  },
  {
    id: 'debug',
    title: 'Debug',
    description: 'Find the bug, explain it, fix it',
    examples: [{ prompt: 'Fix this query: SELECT FirstName, Salary FROM Employes WHERE HireDate > 2024-01-01' }],
  },
  {
    id: 'explain',
    title: 'Explain',
    description: 'Understand any query in plain English',
    examples: [
      { prompt: 'Explain: SELECT DepartmentID, AVG(Salary) FROM Employees GROUP BY DepartmentID HAVING AVG(Salary) > 90000' },
    ],
  },
  {
    id: 'guardrails',
    title: 'Guardrails',
    description: 'Read-only and on-topic by design',
    examples: [{ prompt: 'Delete all orders from 2023' }, { prompt: 'Who won the FIFA World Cup?' }],
  },
];

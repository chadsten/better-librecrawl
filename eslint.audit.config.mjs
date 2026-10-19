// Review rules for npm run audit:complexity: the lint gate plus size,
// complexity and redundancy checks. All warnings, since this is input for a
// code review, not a gate.
import sonarjs from 'eslint-plugin-sonarjs';
import gate from './eslint.config.mjs';

export default [
  ...gate,
  {
    files: ['web/static/**/*.js'],
    plugins: { sonarjs },
    rules: {
      'max-lines': ['warn', 300],
      'max-lines-per-function': ['warn', 150],
      'max-depth': ['warn', 4],
      'max-params': ['warn', 4],
      'sonarjs/cognitive-complexity': ['warn', 15],
      'sonarjs/no-identical-functions': 'warn',
      'sonarjs/no-duplicated-branches': 'warn',
      'sonarjs/no-all-duplicated-branches': 'warn',
      'sonarjs/no-identical-conditions': 'warn',
      'sonarjs/no-identical-expressions': 'warn',
      'sonarjs/no-collapsible-if': 'warn',
      'sonarjs/no-nested-conditional': 'warn',
      'sonarjs/no-small-switch': 'warn',
      'sonarjs/prefer-immediate-return': 'warn',
      'sonarjs/prefer-single-boolean-return': 'warn',
      'sonarjs/no-redundant-boolean': 'warn',
      'sonarjs/no-inverted-boolean-check': 'warn',
      'sonarjs/no-redundant-jump': 'warn',
      'sonarjs/no-redundant-assignments': 'warn',
      'sonarjs/no-useless-catch': 'warn',
      'sonarjs/no-dead-store': 'warn',
      'sonarjs/no-unused-collection': 'warn',
      'sonarjs/no-gratuitous-expressions': 'warn',
      'sonarjs/no-element-overwrite': 'warn',
      'sonarjs/no-ignored-return': 'warn',
    },
  },
];

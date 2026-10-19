/**
 * Virtual Scrolling implementation for large table datasets
 * Only renders visible rows + buffer for smooth scrolling
 *
 * The row height passed in options is only an initial estimate: rows with
 * wrapping text render taller or shorter, so after each render the scroller
 * measures what actually got painted and adapts its estimate. That keeps
 * the spacer math (and therefore the scrollbar) from drifting away from
 * reality on long lists.
 *
 * Columns are defined once per table, in header order. A column's value(row)
 * feeds both its cell and click-to-sort; text(value, row) formats it for
 * display and cell(row) draws a custom cell instead. A column without value()
 * is not sortable.
 */

// Numbers sort as numbers ("9" before "10") and text ignores case
const SORT_COLLATOR = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

function compareSortValues(a, b) {
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    return SORT_COLLATOR.compare(String(a ?? ''), String(b ?? ''));
}

class VirtualScroller {
    constructor(container, options = {}) {
        this.container = container;
        this.tableBody = container.querySelector('tbody');
        this.source = []; // rows in arrival (crawl) order
        this.data = this.source; // rows as displayed: source, or a sorted copy of it

        // Configuration
        this.rowHeight = options.rowHeight || 40; // px per row (adaptive estimate)
        this.buffer = options.buffer || 10; // extra rows to render above/below viewport
        this.columns = options.columns;
        this.decorateRow = options.decorateRow;

        // State
        this.scrollTop = 0;
        this.containerHeight = 0;
        this.visibleStart = -1;
        this.visibleEnd = -1;
        this.renderedLength = -1;
        this.rafPending = false;
        this.sort = null; // { index, direction: 1 | -1 } while a column is sorted
        this.sortPending = false;

        // Create virtual scrolling structure
        this.setupVirtualScroll();
        this.setupSortableHeaders();

        // Bind scroll handler (rAF-throttled so fast scrolling doesn't
        // rebuild the DOM more than once per frame)
        this.handleScroll = this.handleScroll.bind(this);
        this.container.addEventListener('scroll', this.handleScroll, { passive: true });

        // Observe container size changes
        this.resizeObserver = new ResizeObserver(() => {
            this.containerHeight = this.container.clientHeight;
            this.scheduleRender();
        });
        this.resizeObserver.observe(this.container);
    }

    setupVirtualScroll() {
        // Get column count from table header
        const table = this.tableBody.parentElement;
        const headerRow = table.querySelector('thead tr');
        const columnCount = headerRow ? headerRow.children.length : 1;

        // Create spacer rows for virtual scrolling (top and bottom padding)
        this.topSpacer = this.createSpacer(columnCount);
        this.bottomSpacer = this.createSpacer(columnCount);

        // Insert spacers at top and bottom of tbody
        this.tableBody.insertBefore(this.topSpacer, this.tableBody.firstChild);
        this.tableBody.appendChild(this.bottomSpacer);

        // Ensure container can scroll
        this.container.style.overflowY = 'auto';
        this.container.style.overflowX = 'auto';
        this.container.style.position = 'relative';
    }

    setupSortableHeaders() {
        this.headers = Array.from(this.tableBody.parentElement.querySelectorAll('thead th'));
        this.headers.forEach((th, index) => {
            if (!this.columns[index]?.value) return;
            th.classList.add('sortable');
            th.addEventListener('click', (event) => {
                // a click on the resize grip is a resize, not a sort
                if (event.target.closest('.column-resize-grip')) return;
                this.toggleSort(index);
            });
        });
    }

    // Each click on a header steps it through ascending, descending, then
    // back to crawl order
    toggleSort(index) {
        if (!this.sort || this.sort.index !== index) {
            this.sort = { index, direction: 1 };
        } else if (this.sort.direction === 1) {
            this.sort.direction = -1;
        } else {
            this.sort = null;
        }

        this.headers.forEach((th, i) => {
            if (this.sort && i === this.sort.index) {
                th.setAttribute('aria-sort', this.sort.direction === 1 ? 'ascending' : 'descending');
            } else {
                th.removeAttribute('aria-sort');
            }
        });

        this.container.scrollTop = 0;
        this.scrollTop = 0;
        this.applySort();
        this.visibleStart = -1;
        this.render();
    }

    applySort() {
        if (!this.sort) {
            this.data = this.source;
            return;
        }
        // Read each row's value once, then sort (stable, so ties keep crawl order)
        const { value } = this.columns[this.sort.index];
        const direction = this.sort.direction;
        this.data = this.source
            .map(row => ({ row, key: value(row) }))
            .sort((a, b) => direction * compareSortValues(a.key, b.key))
            .map(entry => entry.row);
    }

    createSpacer(columnCount) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = columnCount;
        cell.style.height = '0px';
        cell.style.padding = '0';
        cell.style.border = 'none';
        cell.style.pointerEvents = 'none';
        row.appendChild(cell);
        return row;
    }

    setData(data) {
        this.source = data;
        this.sortPending = false;
        this.applySort();
        // Force a full re-render and clamp the scroll position in case the
        // new data is shorter than where the user had scrolled to
        this.visibleStart = -1;
        this.visibleEnd = -1;
        this.renderedLength = -1;

        const maxScroll = Math.max(0, this.data.length * this.rowHeight - this.containerHeight);
        if (this.scrollTop > maxScroll) {
            this.container.scrollTop = maxScroll;
            this.scrollTop = maxScroll;
        }

        this.render();
    }

    appendData(newData) {
        this.source.push(...newData);
        if (this.sort) {
            // Re-sort at most once per frame, however many rows stream in
            this.sortPending = true;
            this.scheduleRender();
        } else {
            this.render();
        }
    }

    handleScroll() {
        this.scrollTop = this.container.scrollTop;
        this.scheduleRender();
    }

    scheduleRender() {
        if (this.rafPending) return;
        this.rafPending = true;
        requestAnimationFrame(() => {
            this.rafPending = false;
            this.render();
        });
    }

    removeDataRows() {
        const existingRows = Array.from(this.tableBody.children).filter(
            child => child !== this.topSpacer && child !== this.bottomSpacer
        );
        existingRows.forEach(row => row.remove());
    }

    setSpacerHeights(start, end) {
        this.topSpacer.firstChild.style.height = Math.max(0, start * this.rowHeight) + 'px';
        this.bottomSpacer.firstChild.style.height = Math.max(0, (this.data.length - end) * this.rowHeight) + 'px';
    }

    render() {
        if (this.sortPending) {
            this.sortPending = false;
            this.applySort();
            this.visibleStart = -1; // rows may have moved within the window
        }

        const total = this.data.length;

        if (!this.containerHeight) {
            this.containerHeight = this.container.clientHeight;
        }

        if (total === 0) {
            this.removeDataRows();
            this.setSpacerHeights(0, 0);
            this.visibleStart = -1;
            this.visibleEnd = -1;
            this.renderedLength = 0;
            return;
        }

        const firstVisible = Math.floor(this.scrollTop / this.rowHeight);
        const visibleCount = Math.max(1, Math.ceil(this.containerHeight / this.rowHeight));
        const start = Math.max(0, firstVisible - this.buffer);
        const end = Math.min(total, firstVisible + visibleCount + this.buffer);

        if (start === this.visibleStart && end === this.visibleEnd) {
            // Rendered window unchanged. If rows streamed in below it, just
            // grow the bottom spacer so the scrollbar tracks the data —
            // no need to rebuild the visible rows
            if (total !== this.renderedLength) {
                this.renderedLength = total;
                this.setSpacerHeights(start, end);
            }
            return;
        }

        this.visibleStart = start;
        this.visibleEnd = end;
        this.renderedLength = total;
        this.setSpacerHeights(start, end);

        // Rebuild the visible window
        this.removeDataRows();
        const fragment = document.createDocumentFragment();
        for (let i = start; i < end; i++) {
            fragment.appendChild(this.createRow(this.data[i], i));
        }
        this.tableBody.insertBefore(fragment, this.bottomSpacer);

        this.measureRows();
    }

    measureRows() {
        // Adapt the row-height estimate to what actually rendered, so rows
        // that wrap to multiple lines don't make the scroll math drift
        const rows = Array.from(this.tableBody.children).filter(
            child => child !== this.topSpacer && child !== this.bottomSpacer
        );
        if (!rows.length) return;

        let sum = 0;
        for (const row of rows) {
            sum += row.offsetHeight;
        }
        const measured = sum / rows.length;

        if (measured > 0 && Math.abs(measured - this.rowHeight) > 1) {
            // Damped update: converge without jumping around on outlier rows
            this.rowHeight = this.rowHeight * 0.5 + measured * 0.5;
            this.setSpacerHeights(this.visibleStart, this.visibleEnd);
        }
    }

    createRow(rowData, index) {
        const row = document.createElement('tr');
        row.dataset.index = index;

        for (const column of this.columns) {
            if (column.cell) {
                row.appendChild(column.cell(rowData));
                continue;
            }
            // textContent, so crawled-page content can never inject markup
            const cell = document.createElement('td');
            const value = column.value(rowData);
            cell.textContent = column.text ? column.text(value, rowData) : (value ?? '');
            row.appendChild(cell);
        }

        if (this.decorateRow) this.decorateRow(row, rowData);
        return row;
    }

    clear() {
        this.source = [];
        this.data = this.source;
        this.sortPending = false;
        this.visibleStart = -1;
        this.visibleEnd = -1;
        this.renderedLength = 0;
        this.removeDataRows();
        this.setSpacerHeights(0, 0);
    }

    destroy() {
        this.container.removeEventListener('scroll', this.handleScroll);
        this.resizeObserver.disconnect();
    }
}

// Export for use in app.js
window.VirtualScroller = VirtualScroller;

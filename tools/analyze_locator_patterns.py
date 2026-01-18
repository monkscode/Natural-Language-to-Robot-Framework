#!/usr/bin/env python3
"""
Locator Strategy Pattern Analysis Tool

Analyzes workflow_metrics.jsonl to identify patterns in locator strategies,
helping optimize the framework's element finding approaches.

Usage:
    python tools/analyze_locator_patterns.py
    python tools/analyze_locator_patterns.py --last 10  # Analyze last 10 workflows
    python tools/analyze_locator_patterns.py --domain github.com  # Filter by domain
"""

import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Any, Optional


# Strategy mapping (depth -> name)
STRATEGY_MAP = {
    0: 'LLM Candidate',
    1: 'Element Data',
    2: 'Agent Candidate',
    3: 'Collection',
    4: 'Text First',
    5: 'Semantic',
    6: 'Coordinate Fallback'
}

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'


def load_metrics(metrics_file: Path, last_n: Optional[int] = None) -> List[Dict]:
    """Load workflow metrics from JSONL file."""
    if not metrics_file.exists():
        print(f"{Colors.RED}Error: Metrics file not found: {metrics_file}{Colors.END}")
        return []
    
    workflows = []
    with open(metrics_file, 'r') as f:
        for line in f:
            if line.strip():
                try:
                    workflows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    
    if last_n:
        workflows = workflows[-last_n:]
    
    return workflows


def analyze_approach_distribution(workflows: List[Dict]) -> Dict[int, int]:
    """Analyze distribution of locator approaches by fallback depth."""
    depth_counts = defaultdict(int)
    
    for workflow in workflows:
        metrics = workflow.get('element_approach_metrics') or []
        for elem in metrics:
            depth = elem.get('fallback_depth')
            if depth is not None:
                depth_counts[depth] += 1
    
    return dict(depth_counts)


def analyze_domain_patterns(workflows: List[Dict]) -> Dict[str, Dict]:
    """Analyze patterns by domain."""
    domain_stats = defaultdict(lambda: {
        'total_elements': 0,
        'depth_distribution': defaultdict(int),
        'success_rate': 0,
        'avg_depth': 0
    })
    
    for workflow in workflows:
        metrics = workflow.get('element_approach_metrics') or []
        for elem in metrics:
            domain = elem.get('url_domain', 'unknown')
            depth = elem.get('fallback_depth', 0)
            success = elem.get('success', False)
            
            domain_stats[domain]['total_elements'] += 1
            domain_stats[domain]['depth_distribution'][depth] += 1
            if success:
                domain_stats[domain]['success_rate'] += 1
    
    # Calculate averages
    for domain, stats in domain_stats.items():
        if stats['total_elements'] > 0:
            stats['success_rate'] = stats['success_rate'] / stats['total_elements'] * 100
            total_depth = sum(d * c for d, c in stats['depth_distribution'].items())
            stats['avg_depth'] = total_depth / stats['total_elements']
    
    return dict(domain_stats)


def analyze_element_characteristics(workflows: List[Dict]) -> Dict:
    """Analyze element characteristics that affect locator strategy."""
    characteristics = {
        'with_id': {'count': 0, 'avg_depth': 0, 'depths': []},
        'without_id': {'count': 0, 'avg_depth': 0, 'depths': []},
        'with_text': {'count': 0, 'avg_depth': 0, 'depths': []},
        'without_text': {'count': 0, 'avg_depth': 0, 'depths': []},
        'in_iframe': {'count': 0, 'avg_depth': 0, 'depths': []},
        'collections': {'count': 0, 'avg_depth': 0, 'depths': []},
    }
    
    for workflow in workflows:
        metrics = workflow.get('element_approach_metrics') or []
        for elem in metrics:
            depth = elem.get('fallback_depth', 0)
            
            if elem.get('has_id'):
                characteristics['with_id']['count'] += 1
                characteristics['with_id']['depths'].append(depth)
            else:
                characteristics['without_id']['count'] += 1
                characteristics['without_id']['depths'].append(depth)
            
            if elem.get('has_text_content'):
                characteristics['with_text']['count'] += 1
                characteristics['with_text']['depths'].append(depth)
            else:
                characteristics['without_text']['count'] += 1
                characteristics['without_text']['depths'].append(depth)
            
            if elem.get('is_in_iframe'):
                characteristics['in_iframe']['count'] += 1
                characteristics['in_iframe']['depths'].append(depth)
            
            if elem.get('is_collection'):
                characteristics['collections']['count'] += 1
                characteristics['collections']['depths'].append(depth)
    
    # Calculate averages
    for key, data in characteristics.items():
        if data['depths']:
            data['avg_depth'] = sum(data['depths']) / len(data['depths'])
        del data['depths']  # Clean up
    
    return characteristics


def generate_recommendations(
    depth_dist: Dict[int, int],
    domain_stats: Dict[str, Dict],
    characteristics: Dict
) -> List[str]:
    """Generate actionable recommendations based on analysis."""
    recommendations = []
    
    total_elements = sum(depth_dist.values())
    if total_elements == 0:
        return ["No data available for analysis. Run some test cases first."]
    
    # Check fallback usage
    fallback_count = depth_dist.get(6, 0)
    fallback_pct = (fallback_count / total_elements) * 100 if total_elements > 0 else 0
    
    if fallback_pct > 20:
        recommendations.append(
            f"⚠️ HIGH FALLBACK RATE ({fallback_pct:.1f}%): "
            "Consider improving element identifiability on tested pages. "
            "Look for missing IDs, aria-labels, or test-ids."
        )
    elif fallback_pct > 10:
        recommendations.append(
            f"📊 MODERATE FALLBACK RATE ({fallback_pct:.1f}%): "
            "Some elements lack good identifiers. Review failed cases."
        )
    else:
        recommendations.append(
            f"✅ LOW FALLBACK RATE ({fallback_pct:.1f}%): "
            "Framework is performing well with early-stage strategies."
        )
    
    # Check optimal strategy usage
    optimal_count = depth_dist.get(0, 0) + depth_dist.get(1, 0)
    optimal_pct = (optimal_count / total_elements) * 100 if total_elements > 0 else 0
    
    if optimal_pct > 80:
        recommendations.append(
            f"🎯 EXCELLENT: {optimal_pct:.1f}% of elements found with optimal strategies (depth 0-1)."
        )
    elif optimal_pct > 50:
        recommendations.append(
            f"👍 GOOD: {optimal_pct:.1f}% of elements found with optimal strategies. "
            "Room for improvement."
        )
    else:
        recommendations.append(
            f"🔧 NEEDS WORK: Only {optimal_pct:.1f}% using optimal strategies. "
            "Review element identification approach."
        )
    
    # Check ID availability impact
    with_id = characteristics.get('with_id', {})
    without_id = characteristics.get('without_id', {})
    
    if with_id.get('count', 0) > 0 and without_id.get('count', 0) > 0:
        id_depth = with_id.get('avg_depth', 0)
        no_id_depth = without_id.get('avg_depth', 0)
        
        if no_id_depth - id_depth > 2:
            recommendations.append(
                f"💡 ID IMPACT: Elements with IDs average depth {id_depth:.1f}, "
                f"without IDs average {no_id_depth:.1f}. "
                "Adding IDs to elements significantly improves locator quality."
            )
    
    # Domain-specific recommendations
    for domain, stats in domain_stats.items():
        if stats['avg_depth'] > 4 and stats['total_elements'] >= 3:
            recommendations.append(
                f"🌐 DOMAIN '{domain}': High avg depth ({stats['avg_depth']:.1f}). "
                "Consider adding custom locator hints for this domain."
            )
    
    return recommendations


def print_report(
    workflows: List[Dict],
    depth_dist: Dict[int, int],
    domain_stats: Dict[str, Dict],
    characteristics: Dict,
    recommendations: List[str]
):
    """Print formatted analysis report."""
    total_elements = sum(depth_dist.values())
    
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"{Colors.BOLD}   LOCATOR STRATEGY PATTERN ANALYSIS REPORT{Colors.END}")
    print(f"{Colors.BOLD}{'='*70}{Colors.END}")
    print(f"   Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Workflows analyzed: {len(workflows)}")
    print(f"   Total elements: {total_elements}")
    
    # Strategy Distribution
    print(f"\n{Colors.CYAN}{'─'*70}{Colors.END}")
    print(f"{Colors.BOLD}📊 STRATEGY DISTRIBUTION{Colors.END}")
    print(f"{Colors.CYAN}{'─'*70}{Colors.END}")
    
    for depth in range(7):
        count = depth_dist.get(depth, 0)
        pct = (count / total_elements * 100) if total_elements > 0 else 0
        bar_len = int(pct / 2)
        bar = '█' * bar_len
        
        # Color based on depth
        if depth <= 1:
            color = Colors.GREEN
        elif depth <= 3:
            color = Colors.BLUE
        elif depth <= 5:
            color = Colors.YELLOW
        else:
            color = Colors.RED
        
        strategy_name = STRATEGY_MAP.get(depth, f'Unknown ({depth})')
        print(f"   {depth}: {strategy_name:20} {color}{bar:25} {count:4} ({pct:5.1f}%){Colors.END}")
    
    # Domain Analysis
    if domain_stats:
        print(f"\n{Colors.CYAN}{'─'*70}{Colors.END}")
        print(f"{Colors.BOLD}🌐 DOMAIN ANALYSIS{Colors.END}")
        print(f"{Colors.CYAN}{'─'*70}{Colors.END}")
        
        for domain, stats in sorted(domain_stats.items(), key=lambda x: -x[1]['total_elements']):
            print(f"   {domain}:")
            print(f"      Elements: {stats['total_elements']}, "
                  f"Avg Depth: {stats['avg_depth']:.1f}, "
                  f"Success: {stats['success_rate']:.0f}%")
    
    # Element Characteristics
    print(f"\n{Colors.CYAN}{'─'*70}{Colors.END}")
    print(f"{Colors.BOLD}🔍 ELEMENT CHARACTERISTICS IMPACT{Colors.END}")
    print(f"{Colors.CYAN}{'─'*70}{Colors.END}")
    
    for key, data in characteristics.items():
        if data['count'] > 0:
            print(f"   {key.replace('_', ' ').title():20}: "
                  f"{data['count']:4} elements, Avg Depth: {data['avg_depth']:.2f}")
    
    # Recommendations
    print(f"\n{Colors.CYAN}{'─'*70}{Colors.END}")
    print(f"{Colors.BOLD}💡 RECOMMENDATIONS{Colors.END}")
    print(f"{Colors.CYAN}{'─'*70}{Colors.END}")
    
    for rec in recommendations:
        print(f"   {rec}")
    
    print(f"\n{Colors.BOLD}{'='*70}{Colors.END}\n")


def main():
    parser = argparse.ArgumentParser(
        description='Analyze locator strategy patterns from workflow metrics'
    )
    parser.add_argument(
        '--last', '-n', type=int, default=None,
        help='Analyze only the last N workflows'
    )
    parser.add_argument(
        '--domain', '-d', type=str, default=None,
        help='Filter by specific domain'
    )
    parser.add_argument(
        '--metrics-file', '-f', type=str,
        default='logs/workflow_metrics.jsonl',
        help='Path to metrics file'
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Output as JSON instead of formatted report'
    )
    
    args = parser.parse_args()
    
    # Resolve metrics file path
    metrics_file = Path(args.metrics_file)
    if not metrics_file.is_absolute():
        # Try relative to script location
        script_dir = Path(__file__).parent.parent
        metrics_file = script_dir / args.metrics_file
    
    # Load workflows
    workflows = load_metrics(metrics_file, args.last)
    
    if not workflows:
        print(f"{Colors.RED}No workflows found to analyze.{Colors.END}")
        return
    
    # Filter by domain if specified
    if args.domain:
        filtered = []
        for w in workflows:
            metrics = w.get('element_approach_metrics') or []
            if any(e.get('url_domain') == args.domain for e in metrics):
                filtered.append(w)
        workflows = filtered
    
    # Run analysis
    depth_dist = analyze_approach_distribution(workflows)
    domain_stats = analyze_domain_patterns(workflows)
    characteristics = analyze_element_characteristics(workflows)
    recommendations = generate_recommendations(depth_dist, domain_stats, characteristics)
    
    if args.json:
        output = {
            'workflows_analyzed': len(workflows),
            'total_elements': sum(depth_dist.values()),
            'strategy_distribution': {
                STRATEGY_MAP.get(k, f'depth_{k}'): v for k, v in depth_dist.items()
            },
            'domain_stats': domain_stats,
            'element_characteristics': characteristics,
            'recommendations': recommendations
        }
        print(json.dumps(output, indent=2))
    else:
        print_report(workflows, depth_dist, domain_stats, characteristics, recommendations)


if __name__ == '__main__':
    main()

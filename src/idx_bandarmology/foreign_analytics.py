import numpy as np
import pandas as pd
from hmmlearn import hmm
from statsmodels.tsa.api import VAR
import networkx as nx
from networkx.algorithms.community import louvain_communities

def compute_hmm_regime(net_flows: list, n_states: int = 3) -> dict:
    """Mengidentifikasi regime akumulasi/distribusi menggunakan Hidden Markov Model."""
    if len(net_flows) < 10:
        return {"states": [], "probabilities": []}
        
    X = np.array(net_flows).reshape(-1, 1)
    
    try:
        # Model HMM Gaussian
        model = hmm.GaussianHMM(n_components=n_states, covariance_type="full", n_iter=100, random_state=42)
        model.fit(X)
        
        raw_states = model.predict(X)
        raw_probs = model.predict_proba(X)
        
        # Mapping state deterministik berdasarkan mean foreign_net
        means = model.means_.flatten()
        sorted_indices = np.argsort(means)
        
        mapping = {
            int(sorted_indices[0]): 0,
            int(sorted_indices[1]): 1,
            int(sorted_indices[2]): 2
        }
        
        mapped_states = [mapping[int(s)] for s in raw_states]
        mapped_probs = raw_probs[:, sorted_indices]
        
        return {
            "states": mapped_states,
            "probabilities": mapped_probs.tolist()
        }
    except Exception:
        # Fallback jika matriks kovarians tidak valid (data terlalu datar/konstan)
        # Kembalikan state netral (1) agar UI tidak crash
        neutral_states = [1] * len(net_flows)
        neutral_probs = [[0.0, 1.0, 0.0]] * len(net_flows)
        return {"states": neutral_states, "probabilities": neutral_probs}
        
def compute_var_irf(foreign_net: list, returns: list, lags: int = 2, horizon: int = 5) -> dict:
    """Menghitung Impulse Response dari intervensi asing ke harga saham (VAR)."""
    if len(foreign_net) != len(returns) or len(foreign_net) < 20:
        return {}
        
    df = pd.DataFrame({
        "foreign": foreign_net,
        "ret": returns
    }).dropna()
    
    model = VAR(df)
    try:
        results = model.fit(maxlags=lags)
        irf = results.irf(horizon)
        
        # Ekstrak efek shock 'foreign' (indeks 0) terhadap 'ret' (indeks 1)
        irfs = irf.irfs[:, 1, 0]
        stderr = irf.stderr()[:, 1, 0]
        
        # Hitung Confidence Interval absolut 95% (Z-score 1.96)
        lower_bound = irfs - 1.96 * stderr
        upper_bound = irfs + 1.96 * stderr
        
        return {
            "foreign_shock_to_ret": irfs.tolist(),
            "lower_bound": lower_bound.tolist(),
            "upper_bound": upper_bound.tolist()
        }
    except Exception:
        # Menghindari crash jika matriks singular (data terlalu datar)
        return {}

def compute_foreign_hhi(broker_net_values: list) -> float:
    """Menghitung Herfindahl-Hirschman Index untuk konsentrasi broker."""
    if not broker_net_values:
        return 0.0
    
    # Ambil nilai absolut untuk mengukur dominasi volume (baik beli maupun jual)
    abs_vals = np.abs(broker_net_values)
    total = np.sum(abs_vals)
    
    if total == 0:
        return 0.0
        
    shares = abs_vals / total
    hhi = np.sum(shares ** 2)
    return float(hhi)

def compute_broker_heatmap(micro_rows: list, top_n: int = 15):
    """Membangun matriks Z-Score untuk Calendar Heatmap dengan Vektorisasi"""
    if not micro_rows:
        return {"x_dates": [], "y_brokers": [], "matrix_data": []}
        
    df = pd.DataFrame(micro_rows, columns=["date", "broker_code", "net_value"])
    
    # Ambil Top N broker paling aktif (berdasarkan total absolut net value)
    top_brokers = df.groupby("broker_code")["net_value"].apply(lambda x: x.abs().sum()).nlargest(top_n).index
    df_top = df[df["broker_code"].isin(top_brokers)]
    
    # Pivot (Long to Wide) dan isi kosong dengan 0
    pivot = df_top.pivot_table(index="date", columns="broker_code", values="net_value", fill_value=0)
    
    # --- Vektorisasi Z-Score C-Level ---
    means = pivot.mean()
    stds = pivot.std()
    
    # Hindari pembagian nol
    safe_stds = stds.replace(0, 1) 
    
    # Kalkulasi matriks serentak
    pivot = (pivot - means) / safe_stds
    
    # Kembalikan broker stagnan (std=0) menjadi murni 0.0
    pivot.loc[:, stds == 0] = 0.0
    # -----------------------------------
            
    # Clip (Winsorize batas ekstrem) ke [-1, 1] agar warna matriks seimbang
    pivot = pivot.clip(lower=-1, upper=1)
    
    # Format khusus untuk ECharts Heatmap: [[x_idx, y_idx, value], ...]
    x_dates = [str(d) for d in pivot.index]
    y_brokers = pivot.columns.tolist()
    matrix_data = []
    
    for i, date_val in enumerate(x_dates):
        for j, broker in enumerate(y_brokers):
            val = float(pivot.iloc[i, j])
            matrix_data.append([i, j, round(val, 3)])
            
    return {"x_dates": x_dates, "y_brokers": y_brokers, "matrix_data": matrix_data}

def compute_broker_network(micro_rows: list, lookback_days: int, min_active_ratio: float = 0.2, corr_threshold: float = 0.65):
    """Membangun matriks korelasi dan Network Graph berbasis Louvain Clustering"""
    if not micro_rows:
        return {"nodes": [], "links": [], "categories": []}
        
    df = pd.DataFrame(micro_rows, columns=["date", "broker_code", "net_value"])
    pivot = df.pivot_table(index="date", columns="broker_code", values="net_value").fillna(0)
    
    # 1. Filter: Minimum Active Days (Buang broker berisik/pasif)
    min_days = int(lookback_days * min_active_ratio)
    active_counts = (pivot != 0).sum()
    valid_brokers = active_counts[active_counts >= min_days].index
    pivot = pivot[valid_brokers]
    
    # 2. Filter: Zero Variance
    pivot = pivot.loc[:, pivot.std() > 0]
    
    if pivot.shape[1] < 2:
        return {"nodes": [], "links": [], "categories": []}
        
    # 3. Pearson Correlation
    corr_matrix = pivot.corr()
    
    # 4. Membangun Jaringan (NetworkX)
    G = nx.Graph()
    brokers = corr_matrix.columns
    for i in range(len(brokers)):
        for j in range(i + 1, len(brokers)):
            b1, b2 = brokers[i], brokers[j]
            weight = corr_matrix.iloc[i, j]
            # Hanya buat garis (edge) jika korelasi sangat kuat
            if abs(weight) >= corr_threshold:
                G.add_edge(b1, b2, weight=weight)
                
    if len(G.nodes) == 0:
        return {"nodes": [], "links": [], "categories": []}
        
    # 5. Kalkulasi Node Centrality & Louvain Communities
    centrality = nx.degree_centrality(G)
    
    try:
        communities = louvain_communities(G, weight='weight')
        raw_partition = {}
        for i, comm in enumerate(communities):
            for node in comm:
                raw_partition[node] = i
    except Exception:
        raw_partition = {node: 0 for node in G.nodes()}
        
    # PERBAIKAN: Memastikan index kategori berurutan (0, 1, 2...) agar aman untuk ECharts
    unique_raw_clusters = sorted(list(set(raw_partition.values())))
    cluster_mapping = {old: new for new, old in enumerate(unique_raw_clusters)}
    
    partition = {node: cluster_mapping[old_val] for node, old_val in raw_partition.items()}
    
    unique_clusters = sorted(list(set(partition.values())))
    categories = [{"name": f"Syndicate {c+1}"} for c in unique_clusters]
    
    nodes = []
    for node in G.nodes():
        size = 10 + (centrality.get(node, 0) * 50) 
        nodes.append({
            "id": str(node),
            "name": str(node),
            "symbolSize": round(size, 2),
            "category": partition.get(node, 0)
        })
        
    links = []
    for u, v, data in G.edges(data=True):
        links.append({
            "source": str(u),
            "target": str(v),
            "value": round(float(data['weight']), 3)
        })
        
    return {"nodes": nodes, "links": links, "categories": categories}

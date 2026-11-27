# Estimativa de custos

O painel tem um **simulador financeiro interativo** (*Admin > Simulador financeiro*). Este documento
resume as premissas.

## Classificação das fontes
| Valor | Tipo | Fonte |
|---|---|---|
| Tamanho do vídeo 720p/60 s/1,5 Mbps ≈ 11,25 MB | **Calculado** | 1,5 Mbit/s × 60 s ÷ 8 |
| Replay da demo: 1,2 MB (720p) e 2,8 MB (480p + marca d'água) | **Medido** | cena sintética simples; vídeo real comprime menos |
| Cloudflare R2: US$ 0,015/GB-mês, classe A US$ 4,50/milhão, classe B US$ 0,36/milhão, egress sem cobrança, faixa gratuita de 10 GB-mês | **Referência pública** | página de preços do R2 (valores do conhecimento do desenvolvedor; **confirmar** em https://developers.cloudflare.com/r2/pricing/ antes de decidir) |
| Taxa Pix 0,99% | **Hipotético** | a página do MP lida não informava a taxa; confirmar no contrato |
| Preço R$ 5,00, conversão 40%, 12 solicitações/dia/câmera, repasse 30% | **Hipotético** | premissas de negócio |
| Edge R$ 120/mês (amortização de mini-PC + energia), servidor central R$ 60/mês | **Hipotético** | |

## Exemplo (valores padrão do simulador)
3 estabelecimentos × 4 câmeras × 12 solicitações/dia × 40% de conversão ≈ **1.728 replays vendidos/mês**

| Item | Mensal |
|---|---|
| Faturamento bruto | R$ 8.640,00 |
| Taxas Pix (0,99%) | − R$ 85,54 |
| Repasse aos estabelecimentos (30%) | − R$ 2.566,34 |
| Infraestrutura | − R$ 420,00 |
| R2 (≈0,6 GB médios + operações) | − R$ 0,14 |
| **Margem estimada** | **≈ R$ 5.568 (64%)** |
| Custo operacional por replay vendido | ≈ R$ 0,24 |
| Ponto de equilíbrio | ≈ 122 replays/mês |

## Comparativo de infraestrutura
1. **Edge local + R2 só para pagos (adotado).** O custo é dominado pelo hardware local. Upload e
   armazenamento ficam desprezíveis porque só ~40% dos replays sobem, ficam no máximo 24 h e o
   egress do R2 não é cobrado.
2. **Edge local sem nuvem de vídeo** (servir direto do mini-PC via túnel). Tem o menor custo, mas
   depende do upload do estabelecimento e da disponibilidade do mini-PC.
3. **Tudo na nuvem** (enviar o stream contínuo). Precisa de upload contínuo de vários Mbps por
   câmera, 24 h por dia, e de servidores de ingestão. É de longe o mais caro por replay e não é
   recomendado.

> Nenhum valor acima é benchmark ou cotação fechada. Atualize as referências antes de qualquer decisão.
